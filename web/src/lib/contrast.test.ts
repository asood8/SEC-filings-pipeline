/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

/**
 * Checks the theme colours in index.css against WCAG 2.2 AA contrast minimums, in both themes:
 * 4.5:1 for text, 3:1 for the outlines of controls and the focus ring (WCAG 1.4.3, 1.4.11).
 * Changing a colour so that a pair falls short fails this test.
 */

type Theme = Record<string, string>

function colours(block: string): Theme {
  return Object.fromEntries(
    [...block.matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})\b/gi)].map((m) => [
      m[1],
      m[2],
    ]),
  )
}

const css = readFileSync(`${__dirname}/../index.css`, 'utf8')
const [lightPart, darkPart] = css.split('@media (prefers-color-scheme: dark)')
const light = colours(lightPart)
const dark = {
  ...light,
  ...colours(darkPart.slice(0, darkPart.indexOf('\n}'))),
}

/** WCAG relative luminance of a #rrggbb colour. */
function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const TEXT = 4.5
const NON_TEXT = 3

// [foreground, background, minimum]: every pairing the stylesheets use.
const PAIRS: [string, string, number][] = [
  ['text', 'bg', TEXT],
  ['text', 'surface', TEXT], // the header, button labels
  ['text', 'accent-soft', TEXT], // a selected filter or company
  ['text', 'warn-bg', TEXT], // notices
  ['muted', 'bg', TEXT], // secondary text, table headers
  ['muted', 'surface', TEXT], // navigation links
  ['muted', 'accent-soft', TEXT], // company names in a selected chip
  ['muted', 'warn-bg', TEXT],
  ['accent', 'bg', TEXT], // links
  ['accent', 'surface', TEXT],
  ['accent', 'accent-soft', TEXT], // badges
  ['accent', 'warn-bg', TEXT], // links in notices
  ['control-border', 'bg', NON_TEXT], // inputs, selects, buttons
  ['control-border', 'surface', NON_TEXT], // the header's search box
  ['accent', 'surface', NON_TEXT], // the focus ring
  ['accent', 'accent-soft', NON_TEXT], // the focus ring on a selected filter
]

describe.each([
  ['light', light],
  ['dark', dark],
])('the %s theme', (_name, theme) => {
  it.each(PAIRS)('%s on %s reaches %s:1', (fg, bg, minimum) => {
    expect(theme[fg], `--${fg}`).toMatch(/^#/)
    expect(theme[bg], `--${bg}`).toMatch(/^#/)
    expect(contrast(theme[fg], theme[bg])).toBeGreaterThanOrEqual(minimum)
  })
})

describe('contrast', () => {
  it('matches the WCAG formula at its extremes', () => {
    expect(contrast('#000000', '#ffffff')).toBeCloseTo(21)
    expect(contrast('#777777', '#777777')).toBe(1)
  })

  it('reads both themes from the stylesheet', () => {
    expect(light.bg).toBe('#ffffff')
    expect(dark.bg).not.toBe(light.bg)
  })
})
