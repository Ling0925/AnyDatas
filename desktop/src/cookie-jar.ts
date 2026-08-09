export class CookieJar {
  readonly #cookies = new Map<string, string>()

  capture(setCookieHeaders: readonly string[]): void {
    for (const header of setCookieHeaders) {
      const [pair = "", ...attributes] = header.split(";")
      const separator = pair.indexOf("=")
      if (separator <= 0) {
        continue
      }
      const name = pair.slice(0, separator).trim()
      const value = pair.slice(separator + 1).trim()
      const expiresNow = attributes.some(
        (attribute) => attribute.trim().toLowerCase() === "max-age=0",
      )
      if (value.length === 0 || expiresNow) {
        this.#cookies.delete(name)
      } else {
        this.#cookies.set(name, value)
      }
    }
  }

  header(): string | undefined {
    if (this.#cookies.size === 0) {
      return undefined
    }
    return [...this.#cookies].map(([name, value]) => `${name}=${value}`).join("; ")
  }
}
