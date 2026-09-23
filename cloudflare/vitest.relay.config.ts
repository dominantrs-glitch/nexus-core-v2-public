import { defineConfig } from "vitest/config";
import { cloudflareTest, readD1Migrations } from "@cloudflare/vitest-plugin";
import {execFileSync} from "node:child_process";
import {resolve} from "node:path";
const nativeFixture=JSON.parse(execFileSync(resolve('..',process.platform==='win32'?'.venv/Scripts/python.exe':'.venv/bin/python'),
  ['-m','tests.native_git_fixture','--learning-bundle'],{cwd:resolve('..'),encoding:'utf8'}));
export default defineConfig(async () => ({
  plugins: [cloudflareTest({ wrangler: { configPath: "./wrangler.relay.jsonc" },
    miniflare: { bindings: { TEST_MIGRATIONS: await readD1Migrations("./relay-migrations"),
      TEST_NATIVE:nativeFixture.source,TEST_NATIVE_LEARNING:nativeFixture } }
  })],
  test: { include: ["test/relay*.test.ts"], testTimeout: 15000 }
}));
