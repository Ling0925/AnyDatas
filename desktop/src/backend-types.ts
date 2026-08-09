export const DESKTOP_PROTOCOL_VERSION = 1

export type BackendMode = "standalone" | "remote"
export type BackendPhase = "unconfigured" | "starting" | "downloading" | "ready" | "failed"

export type BackendSelection =
  | { readonly mode: "standalone" }
  | { readonly mode: "remote"; readonly serverUrl: string }

export type BackendHandshake = {
  readonly service: "anydatas-server"
  readonly serverVersion: string
  readonly protocolVersion: number
  readonly capabilities: string[]
}

export type BackendStatus = {
  readonly mode: BackendMode | null
  readonly phase: BackendPhase
  readonly serverUrl: string | null
  readonly serverVersion: string | null
  readonly protocolVersion: number | null
  readonly message: string
  readonly progress: number | null
}

export type BackendConnection = {
  readonly baseUrl: URL
  readonly desktopToken?: string
  readonly handshake: BackendHandshake
  readonly stop: () => Promise<void>
}

export type BackendProgress = {
  readonly phase: "starting" | "downloading" | "failed"
  readonly message: string
  readonly progress: number | null
}

export type BackendAdapter = {
  readonly connect: (progress: (event: BackendProgress) => void) => Promise<BackendConnection>
}
