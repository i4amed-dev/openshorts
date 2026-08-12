// Lightweight custom-event helper (OpenPanel).
//
// The hosted klippo.one build loads OpenPanel (see index.html), which
// exposes `window.op`. Self-hosted builds, ad-blockers or offline dev simply
// won't have it — every call here is a safe no-op in that case, so analytics
// can never break the app or leak into the self-hosted experience.
//
// Events (name — meaning):
//   - Signup           — account created / signed in
//   - CheckoutStarted  — redirected to Stripe Checkout
//   - Subscribed       — plan activated after checkout
// Prices ride along as ordinary props (e.g. value_usd) for breakdowns.
export function track(event, options) {
  try {
    if (typeof window !== 'undefined' && typeof window.op === 'function') {
      window.op('track', event, (options && options.props) || {});
    }
  } catch (_) {
    /* analytics must never throw into the app */
  }
}
