// Pixel avatars, drawn from a persona's slug.

import { contrast, hex2rgb, hsl2hex, lum, tokenValue } from "./appearance.js";

// ── avatars ─────────────────────────────────────────────────────────────────
// A hired persona keeps one face from roster to running agent (§9.3). Mirrored
// on the vertical axis so the noise reads as a character.
export function hash32(str) {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619) >>> 0; }
  return h >>> 0;
}
// Roster colours are whatever the persona's author typed ("blue", "slate",
// "neon-green"). Canvas ignores a colour it cannot parse and keeps the previous
// `fillStyle`, which painted those sprites in the panel's own colour. These are
// the words the roster uses that CSS does not know; anything else unrecognised
// falls back to the seed.
export const TINT_WORDS = {
  slate: "#94a3b8", amber: "#f59e0b", rose: "#fb7185",
  "neon-green": "#39ff14", "neon-cyan": "#00e5ff", "metallic-blue": "#4a749b",
};

// Canvas only reveals whether it parsed a colour by changing `fillStyle`, so
// two sentinels are set: an accepted value overwrites both identically, a
// rejected one leaves both.
export function asHex(value) {
  if (!value) return null;
  const word = String(value).trim().toLowerCase();
  if (TINT_WORDS[word]) return TINT_WORDS[word];
  const t = document.createElement("canvas").getContext("2d");
  t.fillStyle = "#000000"; t.fillStyle = value; const a = t.fillStyle;
  t.fillStyle = "#ffffff"; t.fillStyle = value; const b = t.fillStyle;
  return (a === b && String(a).charAt(0) === "#") ? a : null;
}

export function hex2hsl(hex) {
  const [r, g, b] = hex2rgb(hex).map((v) => v / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  const l = (mx + mn) / 2;
  if (!d) return [0, 0, l * 100];
  const s = d / (1 - Math.abs(2 * l - 1));
  const h = mx === r ? ((g - b) / d + (g < b ? 6 : 0))
          : mx === g ? (b - r) / d + 2
          : (r - g) / d + 4;
  return [h * 60, s * 100, l * 100];
}

// The hue belongs to the persona; the lightness is adjusted for this panel.
// Near-black roster colours vanish on a dark panel, so the panel picks the
// direction with room, and the clamped start leaves the walk somewhere to go.
export function legible(h, s, l, bg, target) {
  const dir = lum(bg) > 0.4 ? -1 : 1;
  for (let i = 0; i < 120 && contrast(hsl2hex(h, s, l), bg) < target; i++) l += dir;
  return hsl2hex(h, s, Math.min(100, Math.max(0, l)));
}

export function tintFor(seed, color) {
  const bg = tokenValue("--panel-sunk");
  const hex = asHex(color);
  const hsl = hex ? hex2hsl(hex) : [hash32(seed || "anon") % 360, 52, 58];
  return legible(hsl[0], hsl[1], Math.min(80, Math.max(35, hsl[2])), bg, 3);
}

export function drawAvatar(canvas, seed, color) {
  const S = 8, px = 4;
  canvas.width = S * px; canvas.height = S * px;
  const ctx = canvas.getContext("2d");
  let h = hash32(seed || "anon");
  const rnd = () => { h ^= h << 13; h ^= h >>> 17; h ^= h << 5; h >>>= 0; return h / 4294967296; };

  ctx.fillStyle = tokenValue("--panel-sunk");
  ctx.fillRect(0, 0, S * px, S * px);

  const cells = [];
  for (let y = 1; y < S - 1; y++) for (let x = 0; x < S / 2; x++) cells.push(rnd() > 0.5);
  // A seed with very few lit cells reads as a missing picture, so the top is
  // filled deterministically up to a minimum.
  let ink = cells.filter(Boolean).length;
  for (let i = 0; ink < 6 && i < cells.length; i++) if (!cells[i]) { cells[i] = true; ink++; }

  ctx.fillStyle = tintFor(seed, color);
  let i = 0;
  for (let y = 1; y < S - 1; y++) {
    for (let x = 0; x < S / 2; x++, i++) {
      if (cells[i]) {
        ctx.fillRect(x * px, y * px, px, px);
        ctx.fillRect((S - 1 - x) * px, y * px, px, px);
      }
    }
  }
}
