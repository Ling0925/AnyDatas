import { describe, expect, it } from "vitest"
import { isNavigationAllowed } from "./navigation.js"

describe("isNavigationAllowed", () => {
  it.each([
    "http://127.0.0.1:5173/",
    "http://127.0.0.1:5173/login",
    "http://127.0.0.1:5173/file-sources?tab=runs#latest",
  ])("allows same-origin development navigation to %s", (candidate) => {
    // Given
    const policy = { kind: "dev", origin: "http://127.0.0.1:5173" } as const

    // When
    const allowed = isNavigationAllowed(candidate, policy)

    // Then
    expect(allowed).toBe(true)
  })

  it.each([
    "http://localhost:5173/login",
    "https://attacker.example/file-sources",
    "not a URL",
  ])("denies development navigation to %s", (candidate) => {
    // Given
    const policy = { kind: "dev", origin: "http://127.0.0.1:5173" } as const

    // When
    const allowed = isNavigationAllowed(candidate, policy)

    // Then
    expect(allowed).toBe(false)
  })

  it("allows the intended production file root with a fragment", () => {
    // Given
    const policy = {
      kind: "production",
      fileUrl: "file:///Applications/AnyDatas/frontend/dist/index.html",
    } as const

    // When
    const allowed = isNavigationAllowed(
      "file:///Applications/AnyDatas/frontend/dist/index.html#/file-sources",
      policy,
    )

    // Then
    expect(allowed).toBe(true)
  })

  it.each([
    "file:///Applications/AnyDatas/frontend/dist/login",
    "file:///Applications/AnyDatas/frontend/dist/other.html",
    "https://attacker.example/index.html",
  ])("denies production navigation outside the intended file root: %s", (candidate) => {
    // Given
    const policy = {
      kind: "production",
      fileUrl: "file:///Applications/AnyDatas/frontend/dist/index.html",
    } as const

    // When
    const allowed = isNavigationAllowed(candidate, policy)

    // Then
    expect(allowed).toBe(false)
  })
})
