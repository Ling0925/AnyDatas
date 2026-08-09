import { defineConfig } from "vitest/config"

export const config = defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
})

export default config
