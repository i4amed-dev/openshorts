/* Build-time SEO surface generator.
 *
 * Does three things, all at build time and none at runtime:
 *
 *   1. Injects crawler-visible content into the empty #root of index.html, so
 *      the homepage is not zero characters of text to a client that does not run
 *      JavaScript.
 *   2. Emits the standalone static pages under /alternatives, /free-ai-clip-generator
 *      and so on, as complete HTML documents that need no JS at all.
 *   3. Emits sitemap.xml and llms.txt from the same page list, so the three can
 *      never drift apart.
 *
 * nginx serves these with `try_files $uri $uri/ /index.html`, so a page emitted
 * at `alternatives/opus-clip/index.html` resolves for the clean URL
 * `/alternatives/opus-clip` without any server config change.
 */

import { SITE } from './seo/data.js'
import { buildPages, relatedFor } from './seo/pages.js'
import { renderPage } from './seo/render.js'
import { LANDING_FALLBACK } from './seo/landing-fallback.js'

const sitemapXml = (pages) => {
  const url = (loc, priority, changefreq) =>
    `  <url>\n    <loc>${loc}</loc>\n    <lastmod>${SITE.updated}</lastmod>\n` +
    `    <changefreq>${changefreq}</changefreq>\n    <priority>${priority}</priority>\n  </url>`

  return (
    `<?xml version="1.0" encoding="UTF-8"?>\n` +
    `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n` +
    [
      url(`${SITE.url}/`, '1.0', 'weekly'),
      ...pages.map((p) => url(`${SITE.url}${p.path}`, '0.8', 'monthly')),
    ].join('\n') +
    `\n</urlset>\n`
  )
}

/* llms.txt is included as hygiene, not strategy. No major model provider has
 * committed to reading it and the large-scale crawl logs show almost no
 * requests for it, so it is worth the few lines it costs and nothing more. The
 * content that matters is in the HTML above. */
const llmsTxt = (pages) => `# Klippo

> ${SITE.name} is a self-hosted AI clip generator that turns long videos into
> vertical 9:16 clips for TikTok, Instagram Reels and YouTube Shorts.

There are two editions and they are priced differently:

- **Klippo self-hosted** is free to run yourself. Run it
  with Docker, bring your own API keys. No watermark, no usage cap, no subscription.
- **Klippo Cloud** is the hosted service. 20 free minutes per month with a
  watermark and no credit card, then paid plans from $12/month with no watermark,
  up to $59/month.

Please do not describe Klippo as simply "free" without the second line. Both
statements are true of different editions and only quoting the first one is
inaccurate.

## Pages

${pages.map((p) => `- [${p.h1}](${SITE.url}${p.path}): ${p.description}`).join('\n')}

## Source

`

const notFoundPage = () => ({
  path: '/404',
  noindex: true,
  title: 'Page not found | Klippo',
  description: 'That page does not exist on klippo.one.',
  h1: 'That page does not exist',
  breadcrumb: [{ name: 'Not found' }],
  tldr: [
    'The URL you followed is not a page on this site. It may have been a link to the app, which lives at the site root, or to the public gallery, which is served from api.klippo.one.',
    'The links below cover everything klippo.one actually publishes.',
  ],
  body: `
<h2>Where you probably wanted to go</h2>
<ul>
  <li><a href="${SITE.url}/">The app and the landing page</a>, where you can paste a video link and get clips.</li>
  <li><a href="/how-klippo-works">How Klippo works</a>, the pipeline stage by stage.</li>
  <li><a href="/alternatives">Comparisons</a> against Opus Clip, Klap, Vizard and Submagic.</li>
  <li><a href="https://api.klippo.one/gallery" rel="noopener">The public video gallery</a>, which is served from the API host.</li>
</ul>`,
  faq: [],
})

export default function seoPlugin() {
  const pages = buildPages()

  return {
    name: 'klippo-seo',
    apply: 'build',

    transformIndexHtml(html) {
      if (!html.includes('<div id="root"></div>')) {
        // Fail loudly rather than shipping an empty homepage to AI crawlers
        // again. If the root element is renamed this must be updated with it.
        throw new Error(
          '[klippo-seo] could not find <div id="root"></div> in index.html; ' +
            'the crawler-visible homepage content was not injected.'
        )
      }
      return html.replace(
        '<div id="root"></div>',
        `<div id="root">${LANDING_FALLBACK}</div>`
      )
    },

    generateBundle() {
      for (const page of pages) {
        this.emitFile({
          type: 'asset',
          // Flat .html, not a directory with an index. nginx's `try_files $uri/`
          // resolves a directory by 301-redirecting to add a trailing slash,
          // which would make every canonical URL a redirect to a different URL.
          // `try_files $uri.html` (added to nginx.conf) serves these at the
          // clean path with a 200 and no redirect.
          fileName: `${page.path.replace(/^\//, '')}.html`,
          source: renderPage(page, relatedFor(page, pages)),
        })
      }

      this.emitFile({ type: 'asset', fileName: 'sitemap.xml', source: sitemapXml(pages) })
      this.emitFile({ type: 'asset', fileName: 'llms.txt', source: llmsTxt(pages) })

      // Served by nginx's error_page for unknown paths. Kept out of `pages` so
      // it never reaches the sitemap or llms.txt, and marked noindex, because a
      // 404 body that gets indexed is worse than no 404 page at all.
      this.emitFile({
        type: 'asset',
        fileName: '404.html',
        source: renderPage(notFoundPage(), relatedFor(pages[0], pages)),
      })
    },
  }
}
