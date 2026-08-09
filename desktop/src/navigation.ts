export type NavigationPolicy =
  | { readonly kind: "dev"; readonly origin: string }
  | { readonly kind: "production"; readonly fileUrl: string }

export class NavigationPolicyError extends Error {
  override readonly name = "NavigationPolicyError"
}

function assertNever(value: never): never {
  throw new NavigationPolicyError(`Unknown navigation policy: ${String(value)}`)
}

export function isNavigationAllowed(candidate: string, policy: NavigationPolicy): boolean {
  try {
    const candidateUrl = new URL(candidate)
    switch (policy.kind) {
      case "dev":
        return candidateUrl.origin === new URL(policy.origin).origin
      case "production": {
        candidateUrl.hash = ""
        const fileRoot = new URL(policy.fileUrl)
        fileRoot.hash = ""
        return candidateUrl.href === fileRoot.href
      }
      default:
        return assertNever(policy)
    }
  } catch (error) {
    if (error instanceof TypeError) {
      return false
    }
    throw error
  }
}
