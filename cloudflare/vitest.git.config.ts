import { defineConfig } from "vitest/config";
export default defineConfig({ test: { include: ["test/git*.test.ts"], testTimeout: 15000 } });
