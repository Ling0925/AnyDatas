import { describe, expect, it } from "vitest"
import { CookieJar } from "./cookie-jar.js"

describe("CookieJar", () => {
  it("captures every Set-Cookie value by cookie name", () => {
    // Given
    const jar = new CookieJar()

    // When
    jar.capture([
      "session=secret-token; HttpOnly; SameSite=Lax",
      "workspace=alpha; Path=/",
    ])

    // Then
    expect(jar.header()).toBe("session=secret-token; workspace=alpha")
  })

  it("replaces an existing cookie with the latest upstream value", () => {
    // Given
    const jar = new CookieJar()
    jar.capture(["session=old; HttpOnly"])

    // When
    jar.capture(["session=new=value; HttpOnly"])

    // Then
    expect(jar.header()).toBe("session=new=value")
  })

  it("removes a cookie when upstream expires it", () => {
    // Given
    const jar = new CookieJar()
    jar.capture(["session=active; HttpOnly"])

    // When
    jar.capture(["session=; Max-Age=0; HttpOnly"])

    // Then
    expect(jar.header()).toBeUndefined()
  })

  it("clears every cookie when the backend target changes", () => {
    const jar = new CookieJar()
    jar.capture(["session=active; HttpOnly", "workspace=alpha"])

    jar.clear()

    expect(jar.header()).toBeUndefined()
  })
})
