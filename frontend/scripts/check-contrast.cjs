// Parses the tokens out of src/index.css and checks the WCAG AA pairs the two
// themes promise. Run with `npm run check:contrast`. Lives in scripts/ so the
// numbers in the README stay reproducible instead of folklore.
const fs = require('node:fs')
const path = require('node:path')

const css = fs.readFileSync(path.join(__dirname, '..', 'src', 'index.css'), 'utf8')

function parseBlock(selector) {
  const start = css.indexOf(selector + ' {')
  if (start < 0) throw new Error('block not found: ' + selector)
  const open = css.indexOf('{', start)
  const close = css.indexOf('}', open)
  const out = {}
  for (const line of css.slice(open + 1, close).split(';')) {
    const m = line.match(/--([a-z-]+):\s*(.+)$/)
    if (m) out[m[1]] = m[2].trim()
  }
  return out
}

function parseOklch(value) {
  const m = value.match(/oklch\(([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+)%)?\)/)
  if (!m) throw new Error('not an oklch value: ' + value)
  return { L: +m[1], C: +m[2], H: +m[3], alpha: m[4] ? +m[4] / 100 : 1 }
}

function oklchToSrgb({ L, C, H }) {
  const h = (H * Math.PI) / 180
  const a = C * Math.cos(h)
  const b = C * Math.sin(h)
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b
  const s_ = L - 0.0894841775 * a - 1.291485548 * b
  const l = l_ ** 3
  const m = m_ ** 3
  const s = s_ ** 3
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ]
}

const encode = (c) => {
  c = Math.max(0, Math.min(1, c))
  return c <= 0.0031308 ? 12.92 * c : 1.055 * Math.pow(c, 1 / 2.4) - 0.055
}
const linear = (c) => {
  c = Math.max(0, Math.min(1, c))
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4)
}
const luminance = (rgb) => {
  const [r, g, bl] = rgb.map(encode)
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(bl)
}
const ratio = (fg, bg) => {
  const a = luminance(fg)
  const b = luminance(bg)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}
const composite = (fg, bg) => fg.rgb.map((c, i) => c * fg.alpha + bg[i] * (1 - fg.alpha))

function token(block, name, backdrop) {
  const raw = block[name]
  if (!raw) throw new Error('missing token --' + name)
  const parsed = parseOklch(raw)
  return {
    rgb: composite({ rgb: oklchToSrgb(parsed), alpha: parsed.alpha }, backdrop),
  }
}

let failures = 0
function check(label, fg, bg, need) {
  const value = ratio(fg, bg)
  const ok = value >= need
  if (!ok) failures++
  console.log(`${(ok ? 'PASS' : 'FAIL').padEnd(5)}${value.toFixed(2).padStart(6)}  need ${need}  ${label}`)
}

function runTheme(label, selector) {
  const block = parseBlock(selector)
  const white = oklchToSrgb({ L: 1, C: 0, H: 0 })
  const background = token(block, 'background', white).rgb
  const card = token(block, 'card', background).rgb
  const destructive = token(block, 'destructive', background).rgb

  console.log(`\n== ${label} ==`)
  for (const name of ['foreground', 'muted-foreground', 'destructive', 'success', 'warning']) {
    check(`${name} on background`, token(block, name, background).rgb, background, 4.5)
  }
  check('muted-foreground on card', token(block, 'muted-foreground', card).rgb, card, 4.5)
  check(
    'destructive-foreground on destructive',
    token(block, 'destructive-foreground', destructive).rgb,
    destructive,
    4.5,
  )
  check(
    'primary-foreground on primary',
    token(block, 'primary-foreground', token(block, 'primary', background).rgb).rgb,
    token(block, 'primary', background).rgb,
    4.5,
  )

  // Badge tints: bg-<token>/10 over the card. A heavier tint is what used to fail.
  for (const name of ['success', 'warning']) {
    const fg = token(block, name, background).rgb
    const tint = fg.map((c, i) => c * 0.1 + card[i] * 0.9)
    check(`${name} on ${name}/10 badge`, fg, tint, 4.5)
  }

  // Control boundaries and the focus ring answer to 1.4.11, not 1.4.3.
  check('input border on background', token(block, 'input', background).rgb, background, 3.0)
  check('input border on card', token(block, 'input', card).rgb, card, 3.0)
  check('ring on background', token(block, 'ring', background).rgb, background, 3.0)
}

runTheme('LIGHT (default)', ':root')
runTheme('DARK', '.dark')

console.log('\nFAILURES=' + failures)
process.exit(failures === 0 ? 0 : 1)
