/* Single source of truth for Klippo brand identity.
 *
 * Imported by the app (Vite) and by the build-time SEO generator (plain Node) —
 * both run this file as ESM, so keep it dependency-free and free of JSX.
 *
 * The colour lives in dashboard/src/tokens.css as `--color-accent`, expressed in
 * OKLCH so the whole interaction ramp shares one hue; `primary` below is the
 * same colour in hex, for the places that cannot read a CSS variable (favicon
 * markup, OG image generation, theme-color meta, generated static pages).
 * If one changes, change the other: oklch(64.8% 0.11 219) === #219ebc.
 */
export const BRAND = {
  name: 'Klippo',
  domain: 'klippo.one',
  displayDomain: 'Klippo.one',
  slug: 'klippo',
  primary: '#219ebc',
  url: 'https://klippo.one',
  // Support address and repository are deliberately unset rather than invented.
  // Set VITE_SUPPORT_EMAIL / VITE_REPO_URL (or edit here) once they exist; every
  // consumer already handles the empty case by omitting the link.
  supportEmail: import.meta?.env?.VITE_SUPPORT_EMAIL || '',
  repoUrl: import.meta?.env?.VITE_REPO_URL || '',
}

export default BRAND
