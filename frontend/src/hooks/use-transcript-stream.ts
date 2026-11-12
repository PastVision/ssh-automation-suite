/**
 * Live transcript stream for one host run.
 *
 * The backend serves a replay of the transcript tail, then every chunk as it is
 * written, and finally `end` once the host can no longer produce output. Frames
 * carry absolute byte offsets, which are what makes an exactly-once transcript
 * possible: the client tracks the highest offset it has and drops anything at or
 * below it, trimming a frame that straddles the boundary.
 *
 * Only used while a host is still running. Terminal hosts keep the HTTP snapshot
 * path -- the full file is already on disk, so a stream would add nothing.
 */

import { useEffect, useRef, useState } from 'react'

import { wsUrl } from '@/lib/api-client'

/** Matches the backend's replay ceiling; keeps memory bounded on a chatty host. */
const MAX_BUFFER_CHARS = 200_000

const RECONNECT_DELAYS_MS = [500, 1_000, 2_000, 4_000, 8_000]

export type StreamStatus = 'connecting' | 'live' | 'ended' | 'error'

/** An execution-level change pushed over the same socket as the transcript. */
export interface StatusUpdate {
  status: string
  reason: string | null
  cancelRequestedAt: string | null
}

export interface TranscriptStream {
  status: StreamStatus
  content: string
  /** Some output is missing, either from the server or from our own buffer. */
  truncated: boolean
  /** Bytes the server dropped because this client could not keep up. */
  droppedBytes: number
  /** Bytes missing because a frame's range skipped past what we had. */
  missingBytes: number
  error: string | null
  /**
   * The newest execution-level event seen on this socket. The backend delivers
   * status changes to transcript subscribers, which is how a cancelling run is
   * noticed without polling the detail endpoint.
   */
  lastStatus: StatusUpdate | null
}

const INITIAL: TranscriptStream = {
  status: 'connecting',
  content: '',
  truncated: false,
  droppedBytes: 0,
  missingBytes: 0,
  error: null,
  lastStatus: null,
}

type Frame =
  | { type: 'replay'; content: string; start: number; end: number; truncated?: boolean }
  | { type: 'chunk'; content: string; start: number; end: number }
  | {
      type: 'truncated'
      reason: string
      dropped_bytes?: number
      missing_bytes?: number
    }
  | {
      type: 'status'
      status: string
      reason?: string | null
      cancel_requested_at?: string | null
    }
  | { type: 'end' }

const encoder = new TextEncoder()
const decoder = new TextDecoder()

/**
 * Drop the first `count` UTF-8 bytes of `text`.
 *
 * Byte offsets do not map to string indices once non-ASCII text is involved, so
 * the text is re-encoded. The server only ever splits on a character boundary,
 * so the remaining bytes always decode cleanly.
 */
function dropLeadingBytes(text: string, count: number): string {
  if (count <= 0) return text
  const bytes = encoder.encode(text)
  if (count >= bytes.length) return ''
  return decoder.decode(bytes.subarray(count))
}

/**
 * Follow one host run's transcript until it finishes.
 *
 * Reconnection is for a dropped socket, not for a finished host: once `end`
 * arrives or the caller disables the hook, no further attempts are made.
 */
export function useTranscriptStream(
  executionId: number,
  hostRunId: number | null,
  { enabled }: { enabled: boolean },
): TranscriptStream {
  const [stream, setStream] = useState<TranscriptStream>(INITIAL)

  // Highest byte offset already in `content`; the dedupe boundary.
  const receivedEnd = useRef(0)
  const attempt = useRef(0)

  useEffect(() => {
    if (!enabled || hostRunId === null) return

    let socket: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let closedByUs = false
    let finished = false

    receivedEnd.current = 0
    attempt.current = 0

    const append = (text: string) => {
      setStream((current) => {
        let content = current.content + text
        let truncated = current.truncated
        if (content.length > MAX_BUFFER_CHARS) {
          content = content.slice(content.length - MAX_BUFFER_CHARS)
          truncated = true
        }
        return { ...current, content, truncated }
      })
    }

    const connect = () => {
      const next = new WebSocket(
        wsUrl(`/api/executions/${executionId}/hosts/${hostRunId}/ws`),
      )
      socket = next

      next.onopen = () => {
        setStream((current) => ({ ...current, status: 'live', error: null }))
      }

      next.onmessage = (event) => {
        let frame: Frame
        try {
          frame = JSON.parse(event.data as string) as Frame
        } catch {
          return
        }

        if (frame.type === 'end') {
          finished = true
          setStream((current) => ({ ...current, status: 'ended' }))
          return
        }

        if (frame.type === 'truncated') {
          setStream((current) => ({
            ...current,
            truncated: true,
            droppedBytes: current.droppedBytes + (frame.dropped_bytes ?? 0),
            missingBytes: current.missingBytes + (frame.missing_bytes ?? 0),
          }))
          return
        }

        if (frame.type === 'status') {
          setStream((current) => ({
            ...current,
            lastStatus: {
              status: frame.status,
              reason: frame.reason ?? null,
              cancelRequestedAt: frame.cancel_requested_at ?? null,
            },
          }))
          return
        }

        if (frame.type === 'replay') {
          // The replay is the authoritative tail, so it replaces whatever a
          // previous connection accumulated instead of being appended to it.
          // Real content here is the only proof the link works, so it is what
          // restores the retry budget -- resetting on `open` alone would let a
          // server that accepts then drops the socket be retried forever.
          attempt.current = 0
          receivedEnd.current = frame.end
          setStream((current) => ({
            ...current,
            status: 'live',
            content: frame.content,
            truncated: Boolean(frame.truncated) || frame.start > 0,
            error: null,
          }))
          return
        }

        const boundary = receivedEnd.current
        if (frame.end <= boundary) return

        if (frame.start > boundary) {
          // A gap means the bytes in between will never arrive.
          const missing = frame.start - boundary
          const text = frame.content
          receivedEnd.current = frame.end
          setStream((current) => ({
            ...current,
            truncated: true,
            missingBytes: current.missingBytes + missing,
          }))
          append(text)
          return
        }

        if (frame.start < boundary) {
          const trimmed = dropLeadingBytes(frame.content, boundary - frame.start)
          receivedEnd.current = frame.end
          if (trimmed) append(trimmed)
          return
        }

        receivedEnd.current = frame.end
        append(frame.content)
      }

      next.onerror = () => {
        // `onclose` follows and owns the retry decision.
      }

      next.onclose = () => {
        socket = null
        if (closedByUs || finished) return

        const delay = RECONNECT_DELAYS_MS[attempt.current]
        if (delay === undefined) {
          setStream((current) => ({
            ...current,
            status: 'error',
            error: 'The live transcript connection was lost.',
          }))
          return
        }

        attempt.current += 1
        setStream((current) => ({ ...current, status: 'connecting' }))
        retryTimer = setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      closedByUs = true
      if (retryTimer !== null) clearTimeout(retryTimer)
      socket?.close()
      socket = null
    }
  }, [executionId, hostRunId, enabled])

  return stream
}
