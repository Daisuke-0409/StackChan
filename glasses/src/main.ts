/**
 * タチコマ for Even G2 — the glasses entrance to the same one persona.
 *
 * Click starts the G2's four-mic array; click again stops it and sends the
 * raw PCM to the gateway. The format needs no conversion at all: the glasses
 * emit PCM 16kHz s16le mono, which is byte-for-byte what /v1/transcribe
 * accepts — and because it is raw voice, speaker identification runs on it,
 * so the glasses know they are talking to Daisuke the same way the robot
 * does. The reply is drawn on the lens; at home, the robot speaks it too
 * (the chat is issued under the robot's device_id, so the audio lands in its
 * speak_queue exactly as if the head had been touched).
 */
/// <reference types="vite/client" />
import {
  waitForEvenAppBridge,
  TextContainerProperty,
  CreateStartUpPageContainer,
  TextContainerUpgrade,
  OsEventTypeList,
} from '@evenrealities/even_hub_sdk'

const GATEWAY = import.meta.env.VITE_GATEWAY ?? 'https://oo.tail20a9df.ts.net'
const DEVICE_ID = import.meta.env.VITE_DEVICE_ID ?? '80456B4DE03C'

// The packaged .ehpk is uploaded to Even's portal, so the bearer token must
// not be baked into it -- a build for packaging leaves VITE_DEVICE_TOKEN
// empty and the app collects the token from the gateway itself at launch.
//
// What comes back is the glasses' own token, not the one the robot bodies
// carry: it opens /v1/transcribe and /v1/chat and nothing else, so a copy
// of it cannot make the robot speak or rewrite who Tachikoma thinks people
// are. /g2/config answers the tailnet only (Tailscale serve proxies from
// loopback), so reaching the gateway over plain Wi-Fi returns 403 -- if
// this ever stops finding a token, check that GATEWAY is the ts.net name.
let TOKEN = import.meta.env.VITE_DEVICE_TOKEN ?? ''

async function resolveToken(): Promise<boolean> {
  if (TOKEN) return true
  try {
    const response = await fetch(`${GATEWAY}/g2/config`)
    const body = await response.json()
    if (response.ok && typeof body.token === 'string' && body.token) {
      TOKEN = body.token
      return true
    }
  } catch {
    // fall through: unreachable gateway reads the same as no token
  }
  return false
}

const IDLE_TEXT = 'タチコマ\n\nクリック: 話す\nダブルクリック: 終了'
const SAMPLE_RATE = 16000
const MAX_UTTERANCE_BYTES = SAMPLE_RATE * 2 * 15 // 15s guard
const MIN_UTTERANCE_BYTES = SAMPLE_RATE * 2 * 0.4 // shorter is a pocket brush

const bridge = await waitForEvenAppBridge()

const mainText = new TextContainerProperty({
  xPosition: 0,
  yPosition: 0,
  width: 576,
  height: 288,
  borderWidth: 0,
  borderColor: 5,
  paddingLength: 4,
  containerID: 1,
  containerName: 'main',
  content: '起動中…',
  isEventCapture: 1,
})

await bridge.createStartUpPageContainer(
  new CreateStartUpPageContainer({ containerTotalNum: 1, textObject: [mainText] }),
)

void resolveToken().then(ok => {
  show(ok ? IDLE_TEXT : '接続エラー\nゲートウェイに届きません\n(PCとTailscaleを確認)')
})

function show(content: string): void {
  bridge.textContainerUpgrade(
    new TextContainerUpgrade({ containerID: 1, containerName: 'main', content }),
  )
}

// Same envelope gotcha the template documents: CLICK_EVENT is 0 and protobuf
// drops zero-value fields, so the default is resolved only inside an
// existing envelope — otherwise every audio frame would read as a tap.
function eventTypeOf(envelope?: { eventType?: OsEventTypeList }): OsEventTypeList | null {
  if (!envelope) return null
  return envelope.eventType ?? OsEventTypeList.CLICK_EVENT
}

type Phase = 'idle' | 'recording' | 'busy'
let phase: Phase = 'idle'
let chunks: Uint8Array[] = []
let collected = 0
const session = `g2-${Math.random().toString(36).slice(2, 10)}`

// The SDK's own typings warn that audioPcm, having crossed the JSON bridge
// from the host, often arrives as number[] or a base64 string rather than
// the declared Uint8Array. Accept all three; dropping frames here would
// corrupt the utterance silently.
function toPcmBytes(raw: unknown): Uint8Array | null {
  if (raw instanceof Uint8Array) return raw
  if (Array.isArray(raw)) return Uint8Array.from(raw)
  if (typeof raw === 'string') {
    try {
      const decoded = atob(raw)
      const out = new Uint8Array(decoded.length)
      for (let i = 0; i < decoded.length; i++) out[i] = decoded.charCodeAt(i)
      return out
    } catch {
      return null
    }
  }
  return null
}

async function startRecording(): Promise<void> {
  chunks = []
  collected = 0
  phase = 'recording'
  show('● 聞いています…\n\nもう一度クリックで送信')
  await bridge.audioControl(true)
}

async function stopAndSend(): Promise<void> {
  phase = 'busy'
  await bridge.audioControl(false)
  const pcm = new Uint8Array(collected)
  let offset = 0
  for (const chunk of chunks) {
    pcm.set(chunk, offset)
    offset += chunk.length
  }
  chunks = []

  if (pcm.length < MIN_UTTERANCE_BYTES) {
    show(`短すぎました\n\n${IDLE_TEXT}`)
    phase = 'idle'
    return
  }

  try {
    show('認識中…')
    const tr = await fetch(`${GATEWAY}/v1/transcribe`, {
      method: 'POST',
      body: pcm,
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        'Content-Type': 'application/octet-stream',
        'X-Device-Id': DEVICE_ID,
        'X-Sample-Rate': String(SAMPLE_RATE),
      },
    })
    const trBody = await tr.json()
    if (!tr.ok || !trBody.text) {
      show(`聞き取れませんでした\n\n${IDLE_TEXT}`)
      phase = 'idle'
      return
    }

    show(`「${trBody.text}」\n\n考え中…`)
    const ch = await fetch(`${GATEWAY}/v1/chat`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        device_id: DEVICE_ID,
        session_id: session,
        request_id: `r-${Date.now()}`,
        text: trBody.text,
      }),
    })
    const chBody = await ch.json()
    show(ch.ok && chBody.text ? chBody.text : '返事に失敗しました')
  } catch {
    show(`通信エラー\nゲートウェイに届きません\n\n${IDLE_TEXT}`)
  }
  phase = 'idle'
}

const unsubscribe = bridge.onEvenHubEvent(event => {
  // Audio frames stream in while recording; each is a slice of s16le PCM.
  const audio = event.audioEvent
  if (audio?.audioPcm != null && phase === 'recording') {
    const bytes = toPcmBytes(audio.audioPcm)
    if (bytes && bytes.length > 0) {
      chunks.push(bytes)
      collected += bytes.length
      if (collected >= MAX_UTTERANCE_BYTES) {
        void stopAndSend()
      }
    }
    return
  }

  const sysType = eventTypeOf(event.sysEvent)
  const textType = eventTypeOf(event.textEvent)

  if (
    sysType === OsEventTypeList.DOUBLE_CLICK_EVENT ||
    textType === OsEventTypeList.DOUBLE_CLICK_EVENT
  ) {
    if (phase === 'recording') void bridge.audioControl(false)
    bridge.shutDownPageContainer(1)
    return
  }

  if (sysType === OsEventTypeList.CLICK_EVENT || textType === OsEventTypeList.CLICK_EVENT) {
    if (phase === 'idle' && TOKEN) void startRecording()
    else if (phase === 'recording') void stopAndSend()
    // busy: ignore clicks; the reply is on its way
    return
  }

  if (
    sysType === OsEventTypeList.SYSTEM_EXIT_EVENT ||
    sysType === OsEventTypeList.ABNORMAL_EXIT_EVENT
  ) {
    if (phase === 'recording') void bridge.audioControl(false)
    unsubscribe()
  }
})
