#!/usr/bin/env node
// Renders one SDK's symbol list into its reference page Markdown.
//
// Every SDK's extractor hands this the same JSON shape, so every page comes out the same way.
// Capabilities come from sdk-specs: an entry's `capability` slug picks its area and heading.
// Entries with no capability go to SDK utils. Usage:
//
//   node render.mjs <page.json> <capabilities.json> > reference.md
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

/** The heading id the docs site and GitHub give a heading with this text. */
export function slugify(text) {
    return text
        .toLowerCase()
        .replace(/[^\p{L}\p{N}\- ]/gu, '')
        .replace(/ /g, '-')
}

const fence = (code, lang) => '```' + lang + '\n' + code.trim() + '\n```'
const code = (text) => (text ? '`' + String(text).replace(/\s*\n\s*/g, ' ').trim() + '`' : '')
/**
 * Table cell text. Code fences become inline code, paragraphs join with <br />, and pipes are
 * escaped. A pipe inside a code span splits it into spans around `\|`, since the site's MDX shows
 * `\|` inside one span literally.
 */
const cellText = (text) =>
    String(text ?? '')
        .trim()
        .replace(/```\w*\n([\s\S]*?)```/g, (_, c) => '`' + c.trim().replace(/\s*\n\s*/g, '; ') + '`')
        .split(/\n\s*\n/)
        .map((paragraph) => paragraph.replace(/\s*\n\s*/g, ' '))
        .join('<br />')
        .split(/(`[^`]*`)/)
        .map((part) =>
            !part.startsWith('`')
                ? part.replace(/\|/g, '\\|')
                : part.slice(1, -1).split(/\s*\|\s*/).map((x) => '`' + x + '`').join(' \\| ')
        )
        .join('')

/**
 * One details block holding a one-column table: a row per parameter, field or setting with its
 * name, a labeled type and default line, then its description.
 */
function members(title, rows) {
    if (!rows?.length) return null
    const head = { Parameters: 'Parameter', Fields: 'Field', Settings: 'Setting' }[title]
    const lines = rows.map((r) => {
        const labels = [
            r.where && `**Where:** ${r.where}`,
            r.value && `**Value:** ${cellText(code(r.value))}`,
            r.type && `**Type:** ${cellText(code(r.type))}`,
            r.required ? '**Required**' : r.default && `**Default:** ${cellText(code(r.default))}`,
        ].filter(Boolean)
        const cell = [code(r.name), labels.join(' · '), r.description && cellText(r.description)].filter(Boolean).join('<br />')
        return `| ${cell} |`
    })
    return ['<details>', `<summary>${title} (${rows.length})</summary>`, '', `| ${head} |`, '| --- |', ...lines, '', '</details>'].join('\n')
}

const sentence = (text) => (text ? text[0].toUpperCase() + text.slice(1) + (/[.!?]$/.test(text) ? '' : '.') : '')

function example(ex, lang) {
    const label =
        // Source examples are correct usage unless marked as a counter example.
        ex.label === 'incorrect' ? `**Counter example (incorrect).** ${sentence(ex.why)}`.trim() : '**Correct example.**'
    return `${label}\n\n${fence(ex.code, lang)}`
}

function entry(e, level, lang) {
    const out = [`${'#'.repeat(level)} \`${e.qualname}\``]
    if (e.deprecated) out.push(`**Deprecated.** ${e.deprecated}`)
    if (e.availableIn) out.push(`**Available in:** ${e.availableIn}`)
    if (e.description) out.push(e.description)
    if (e.signature) out.push(fence(e.signature, lang))
    const params = members('Parameters', e.params)
    if (params) out.push(params)
    if (e.returns && (e.returns.type || e.returns.description)) {
        out.push(`**Returns** ${code(e.returns.type)}${e.returns.description ? `. ${e.returns.description}` : ''}`.trim())
    }
    for (const r of e.raises ?? []) out.push(`**Raises** ${code(r.type)}. ${r.description ?? ''}`.trim())
    if (e.notes) out.push(e.notes)
    for (const ex of e.examples ?? []) out.push(example(ex, lang))
    if (e.source) out.push(`[Source](${e.source})`)
    return out.join('\n\n')
}

/** Moves every heading down one level, dropping the H1 when asked. Code fences stay as written. */
function nest(markdown, { dropTitle = true } = {}) {
    let inFence = false
    return markdown
        .trim()
        .split('\n')
        .flatMap((line) => {
            if (/^\s*(```|~~~)/.test(line)) inFence = !inFence
            if (inFence || !/^#{1,5} /.test(line)) return [line]
            return dropTitle && line.startsWith('# ') ? [] : ['#' + line]
        })
        .join('\n')
        .replace(/^\n+/, '')
}

function capabilityOf(e, index) {
    const cap = index.capabilities[e.capability]
    if (!cap) throw new Error(`Unknown capability "${e.capability}" on ${e.qualname}`)
    if (slugify(cap.title) !== e.capability) throw new Error(`Title "${cap.title}" doesn't produce slug "${e.capability}"`)
    return cap
}

const order = (index) => (e) => {
    const cap = index.capabilities[e.capability]
    return [index.areas.indexOf(cap.area), Object.keys(index.capabilities).indexOf(e.capability)]
}

function byOrder(entries, index) {
    const key = order(index)
    return [...entries].sort((a, b) => {
        const [x, y] = [key(a), key(b)]
        return x[0] - y[0] || x[1] - y[1]
    })
}

function groups(entries, keyOf) {
    const map = new Map()
    for (const e of entries) map.set(keyOf(e), [...(map.get(keyOf(e)) ?? []), e])
    return map
}

function defaultReference(ref, index, lang) {
    const mapped = ref.entries.filter((e) => e.capability)
    mapped.forEach((e) => capabilityOf(e, index))
    const out = []
    // Each sdk-specs capability is a top-level section, in area order. Its heading's anchor is the shared slug.
    for (const [slug, entries] of groups(byOrder(mapped, index), (e) => e.capability)) {
        out.push(`## ${index.capabilities[slug].title}`, ...entries.map((e) => entry(e, 3, lang)))
    }
    const utils = ref.entries.filter((e) => !e.capability)
    if (utils.length) {
        out.push('## SDK utils')
        for (const [group, entries] of groups(utils, (e) => e.group ?? 'Other')) {
            out.push(`### ${group}`, ...entries.map((e) => entry(e, 4, lang)))
        }
    }
    return out
}

function secondaryReference(ref, index, lang) {
    const mapped = ref.entries.filter((e) => e.capability)
    mapped.forEach((e) => capabilityOf(e, index))
    const entries = [...byOrder(mapped, index), ...ref.entries.filter((e) => !e.capability)]
    return [`## ${ref.title}`, ...(ref.intro ? [ref.intro] : []), ...entries.map((e) => entry(e, 3, lang))]
}

function type(t, lang) {
    if (!t.description && !t.fields?.length && !t.methods?.length) return []
    const out = [`### \`${t.name}\``]
    if (t.description) out.push(t.description)
    const fields = members('Fields', t.fields)
    if (fields) out.push(fields)
    out.push(...(t.methods ?? []).map((m) => entry(m, 4, lang)))
    if (t.source) out.push(`[Source](${t.source})`)
    return [out.join('\n\n')]
}

export function render(page, index) {
    const lang = page.language
    const [first, ...rest] = page.references
    // current: the released major customers install. experimental: a newer major that hasn't shipped.
    if (!['current', 'experimental'].includes(page.status)) throw new Error(`Unknown status "${page.status}"`)
    // Getting started renders before the reference, so its headings would take capability anchors first.
    for (const [, title] of page.gettingStarted.matchAll(/^#{2,6} (.+)$/gm)) {
        if (index.capabilities[slugify(title)]) throw new Error(`Getting started heading "${title}" takes the "${slugify(title)}" anchor`)
    }
    const sections = [
        ['---', `title: ${page.title}`, `sdk: ${page.sdk}`, `version: ${page.version}`, `major: ${page.major}`, `status: ${page.status}`, '---'].join('\n'),
        '## Getting started',
        nest(page.gettingStarted),
        ...defaultReference(first, index, lang),
        ...rest.flatMap((ref) => secondaryReference(ref, index, lang)),
    ]
    const configs = members('Settings', page.configs)
    if (configs) sections.push('## Configs', configs)
    const types = (page.types ?? []).flatMap((t) => type(t, lang))
    if (types.length) sections.push('## Types', ...types)
    return sections.join('\n\n') + '\n'
}

/** The SDK's migration guides as their own page, newest first. Each guide's title becomes an H2. */
export function renderMigration(page) {
    if (!page.migrations?.length) return null
    const head = ['---', `title: ${page.title} migration guides`, `sdk: ${page.sdk}`, 'kind: migration', '---'].join('\n')
    return [head, ...page.migrations.map((guide) => nest(guide, { dropTitle: false }))].join('\n\n') + '\n'
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
    const [pagePath, indexPath, flag] = process.argv.slice(2)
    const read = (p) => JSON.parse(readFileSync(p, 'utf8'))
    process.stdout.write((flag === '--migration' ? renderMigration(read(pagePath)) : render(read(pagePath), read(indexPath))) ?? '')
}
