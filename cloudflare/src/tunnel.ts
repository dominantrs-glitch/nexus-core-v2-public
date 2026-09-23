import { DurableObject } from "cloudflare:workers";
import { BudgetExceeded, budgetFailure, failure, MAX_RESPONSE, reserve, type RelayEnv } from "./relay-common";

type Pending = { resolve: (response: Response) => void; timer: ReturnType<typeof setTimeout> };
const CONNECTOR_IDLE_MS = 90_000;

export class LocalTunnel extends DurableObject<RelayEnv> {
  private pending = new Map<string, Pending>();
  private uploading = false;

  constructor(ctx: DurableObjectState, env: RelayEnv) {
    super(ctx, env);
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  async fetch(request: Request): Promise<Response> {
    const path = new URL(request.url).pathname;
    if (path === "/connect") {
      if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") return failure(400, "websocket_required");
      if (this.liveSocket()) return failure(409, "connector_already_online");
      const pair = new WebSocketPair();
      this.ctx.acceptWebSocket(pair[1]);
      pair[1].serializeAttachment({ connectedAt: Date.now(), retired: false });
      return new Response(null, { status: 101, webSocket: pair[0] });
    }
    if (path === "/request") {
      const body = await request.text();
      const socket = this.liveSocket();
      if (!socket) return failure(503, "pc_offline");
      if (this.pending.size || this.uploading) return failure(503, "pc_busy");
      const id = crypto.randomUUID();
      return new Promise<Response>((resolve) => {
        const timer = setTimeout(() => {
          this.pending.delete(id);
          resolve(failure(504, "pc_timeout"));
        }, 45000);
        this.pending.set(id, { resolve, timer });
        try {
          socket.send(JSON.stringify({ id, body, project: this.env.PROJECT_ID,
            protocol: request.headers.get("MCP-Protocol-Version") }));
        } catch {
          clearTimeout(timer); this.pending.delete(id); resolve(failure(503, "pc_offline"));
        }
      });
    }
    if (path.startsWith("/reply/") && request.method === "POST") {
      const id = path.slice(7);
      const job = this.pending.get(id);
      if (!job) return failure(409, "unknown_or_consumed_request");
      // Consume synchronously before awaiting anything: duplicate delivery cannot win.
      this.pending.delete(id); clearTimeout(job.timer);
      this.uploading = true;
      try {
      const size = Number(request.headers.get("Content-Length"));
      if (!request.headers.has("Content-Length") || !Number.isSafeInteger(size) || size < 0 || size > MAX_RESPONSE) {
        job.resolve(failure(503, "response_limit")); return failure(413, "response_limit");
      }
      try { await reserve(this.env, 0, size); }
      catch (error) {
        if (error instanceof BudgetExceeded) { job.resolve(budgetFailure()); return budgetFailure(); }
        job.resolve(failure(503, "relay_unavailable")); return failure(503, "relay_unavailable");
      }
      const status = Number(request.headers.get("X-Nexus-Status"));
      if (![200, 202, 400, 404, 405, 406, 415, 503].includes(status) || (status === 202 && size !== 0)) {
        job.resolve(failure(503, "invalid_pc_response")); return failure(400, "invalid_pc_response");
      }
      let received = 0;
      const transform = new TransformStream<Uint8Array, Uint8Array>({
        transform(chunk, controller) {
          received += chunk.length;
          if (received > size) throw new Error("response length mismatch");
          controller.enqueue(chunk);
        },
        flush() { if (received !== size) throw new Error("truncated response"); }
      });
      const pumping = request.body ? request.body.pipeTo(transform.writable) : transform.writable.getWriter().close();
      job.resolve(new Response(transform.readable, { status, headers: {
        "Content-Type": "application/json", "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff"
      } }));
      // Keep the upload request alive until the client consumes the stream.
      // Returning 204 early invalidates its request body in workerd.
      try { await pumping; }
      catch { return failure(503, "response_interrupted"); }
      return new Response(null, { status: 204 });
      } finally { this.uploading = false; }
    }
    return failure(404, "not_found");
  }

  webSocketMessage(socket: WebSocket, _message: string | ArrayBuffer) {
    if (socket.deserializeAttachment()?.retired) return;
    // Control messages only originate at the relay. Heartbeats use auto-response.
    socket.close(1008, "unexpected message");
    this.failPending();
  }
  webSocketClose(socket: WebSocket, code: number, reason: string) {
    if (socket.deserializeAttachment()?.retired) return;
    socket.close(code, reason); this.failPending();
  }
  webSocketError(socket: WebSocket) {
    if (socket.deserializeAttachment()?.retired) return;
    socket.close(1011, "connection error"); this.failPending();
  }
  private liveSocket(): WebSocket | undefined {
    let live: WebSocket | undefined;
    for (const socket of this.ctx.getWebSockets()) {
      const meta = socket.deserializeAttachment();
      if (meta?.retired) continue;
      // Auto-response timestamps survive hibernation. Old deployments without an
      // attachment get one grace interval if no heartbeat timestamp exists.
      const now = Date.now();
      if (!meta) socket.serializeAttachment({ connectedAt: now, retired: false });
      const lastSeen = this.ctx.getWebSocketAutoResponseTimestamp(socket)?.getTime()
        ?? meta?.connectedAt ?? now;
      if (socket.readyState === WebSocket.OPEN && now - lastSeen <= CONNECTOR_IDLE_MS) {
        live = socket;
      } else {
        socket.serializeAttachment({ retired: true });
        socket.close(1001, "connector heartbeat expired");
        this.failPending();
      }
    }
    return live;
  }
  private failPending() {
    for (const job of this.pending.values()) { clearTimeout(job.timer); job.resolve(failure(503, "pc_disconnected")); }
    this.pending.clear();
  }
}
