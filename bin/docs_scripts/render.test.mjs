import { test } from 'node:test'
import assert from 'node:assert/strict'
import { render, renderMigration, slugify } from './render.mjs'

const capabilities = {
    areas: ['Events', 'Feature flags', 'Lifecycle'],
    capabilities: {
        capture: { title: 'Capture', area: 'Events' },
        'get-feature-flag': { title: 'Get feature flag', area: 'Feature flags' },
        flush: { title: 'Flush', area: 'Lifecycle' },
    },
}

const capture = {
    qualname: 'Client.capture',
    capability: 'capture',
    signature: 'capture(event: str, **kwargs) -> Optional[str]',
    description: 'Captures an event.',
    params: [
        { name: 'event', type: 'str', required: true, description: 'The event name.' },
        { name: 'timestamp', type: 'datetime | str', default: 'None', description: 'When it happened | UTC.' },
        { name: 'secret_key', default: 'None', description: 'A secret key.\nExample:\n\n```python\nClient(key, secret_key="phx_")\n```' },
    ],
    returns: { type: 'Optional[str]', description: 'The event UUID, or None when it was dropped.' },
    examples: [
        { code: 'client.capture("signed_up", distinct_id="u1")' },
        { label: 'incorrect', why: 'fails because `distinct_id` is keyword-only', code: 'client.capture("u1", "signed_up")' },
        { label: 'correct', code: 'client.capture("signed_up", distinct_id="u1")' },
    ],
}

const page = {
    sdk: 'python',
    title: 'PostHog Python SDK',
    version: '8.0.0',
    major: 8,
    status: 'experimental',
    language: 'python',
    gettingStarted: '# Getting started\n\nInstall it.\n\n## Install\n\n```bash\npip install posthog\n```',
    references: [
        {
            title: 'Client',
            entries: [
                { qualname: 'Client.flush', capability: 'flush', description: 'Sends queued events.' },
                capture,
                { qualname: 'Client.tag', capability: null, group: 'Contexts', description: 'Adds a tag.' },
            ],
        },
        {
            title: 'AsyncClient',
            entries: [{ qualname: 'AsyncClient.capture', capability: 'capture', description: 'Captures an event.' }],
        },
    ],
    configs: [{ name: 'POSTHOG_MW_CAPTURE_EXCEPTIONS', where: 'Django settings', type: 'bool', default: 'True', description: 'Captures exceptions.' }],
    types: [
        {
            name: 'FeatureFlagEvaluations',
            kind: 'class',
            description: 'A snapshot of flag values.',
            fields: [],
            methods: [{ qualname: 'FeatureFlagEvaluations.is_enabled', signature: 'is_enabled(key: str) -> bool', description: 'Checks a flag.' }],
        },
        { name: 'Empty', kind: 'class', description: '', fields: [], methods: [] },
        { name: 'Compression', description: 'Request compression.', fields: [{ name: 'GZIP', value: '"gzip"', description: 'gzip, from the standard library.' }], methods: [] },
    ],
    migrations: ['# Migrating from 7.x to 8.0\n\n## Capture\n\nIt changed.', '# Migrating from 6.x to 7.0\n\nOlder.'],
}

const md = render(page, capabilities)
const h2 = md.split('\n').filter((l) => l.startsWith('## ')).map((l) => l.slice(3))

test('slugify matches heading ids', () => {
    assert.equal(slugify('Get feature flag'), 'get-feature-flag')
    assert.equal(slugify('Get anonymous ID'), 'get-anonymous-id')
    assert.equal(slugify('`Client.capture`'), 'clientcapture')
})

test('top-level headings are capabilities in area order, then structure', () => {
    assert.deepEqual(h2, ['Getting started', 'Capture', 'Flush', 'SDK utils', 'AsyncClient', 'Configs', 'Types'])
})

test('capability headings carry the shared slug', () => {
    assert.match(md, /^## Capture$/m)
    assert.equal(slugify('Capture'), 'capture')
    assert.match(md, /^## Flush$/m)
    assert.doesNotMatch(md, /^## Events$/m)
})

test('unknown capabilities and slug mismatches throw', () => {
    const bad = { ...page, references: [{ title: 'Client', entries: [{ qualname: 'x', capability: 'nope' }] }] }
    assert.throws(() => render(bad, capabilities), /Unknown capability "nope"/)
    const mismatch = { ...capabilities, capabilities: { ...capabilities.capabilities, capture: { title: 'Capture events', area: 'Events' } } }
    assert.throws(() => render(page, mismatch), /slug/)
})

test('a getting started heading that would take a capability slug throws', () => {
    const clash = { ...page, gettingStarted: '# Getting started\n\n## Capture\n\nSend one.' }
    assert.throws(() => render(clash, capabilities), /Getting started heading "Capture" takes the "capture" anchor/)
})

test('an entry renders signature, parameters, returns and labeled examples', () => {
    assert.match(md, /^### `Client\.capture`$/m)
    assert.match(md, /```python\ncapture\(event: str, \*\*kwargs\) -> Optional\[str\]\n```/)
    assert.match(md, /<details>\n<summary>Parameters \(3\)<\/summary>\n\n\| Parameter \|\n\| --- \|\n\| `event`<br \/>\*\*Type:\*\* `str` · \*\*Required\*\*<br \/>The event name\. \|\n/)
    assert.match(md, /^\| `timestamp`<br \/>\*\*Type:\*\* `datetime` \\\| `str` · \*\*Default:\*\* `None`<br \/>When it happened \\\| UTC\. \|$/m)
    assert.match(md, /^\| `secret_key`<br \/>\*\*Default:\*\* `None`<br \/>A secret key\. Example:<br \/>`Client\(key, secret_key="phx_"\)` \|\n\n<\/details>/m)
    assert.doesNotMatch(md, /^#####/m)
    assert.match(md, /\*\*Returns\*\* `Optional\[str\]`\. The event UUID/)
    assert.match(md, /\*\*Correct example\.\*\*\n\n```python\nclient\.capture\("signed_up", distinct_id="u1"\)\n```\n\n\*\*Counter/)
    assert.doesNotMatch(md, /\*\*Example\.\*\*/)
    assert.match(md, /\*\*Counter example \(incorrect\)\.\*\* Fails because `distinct_id` is keyword-only\.\n\n```python/)
    assert.match(md, /\*\*Correct example\.\*\*\n\n```python\nclient\.capture\("signed_up", distinct_id="u1"\)/)
})

test('member names sit in table rows, so they never take a capability anchor', () => {
    const clash = { ...page, references: [{ title: 'Client', entries: [{ qualname: 'Client(...)', capability: 'capture', params: [{ name: 'flush', default: 'None' }] }, { qualname: 'Client.flush', capability: 'flush' }] }] }
    const out = render(clash, capabilities)
    assert.match(out, /^\| `flush`<br \/>\*\*Default:\*\* `None` \|$/m)
    assert.doesNotMatch(out, /^#+ `flush`/m)
})

test('every parameter block sits in a details block, even a short one', () => {
    const one = { ...page, references: [{ title: 'Client', entries: [{ qualname: 'Client.flush', capability: 'flush', params: [{ name: 'timeout', default: '5' }] }] }] }
    assert.match(render(one, capabilities), /<details>\n<summary>Parameters \(1\)<\/summary>\n\n\| Parameter \|\n\| --- \|\n\| `timeout`<br \/>\*\*Default:\*\* `5` \|\n\n<\/details>/)
    assert.equal((md.match(/<details>/g) ?? []).length, (md.match(/<\/details>/g) ?? []).length)
    assert.doesNotMatch(md, /^- `/m)
})

test('unmapped entries land in SDK utils under their group', () => {
    const utils = md.slice(md.indexOf('## SDK utils'))
    assert.match(utils, /^### Contexts\n\n#### `Client\.tag`/m)
})

test('secondary references list entries without area headings', () => {
    const asyncRef = md.slice(md.indexOf('## AsyncClient'), md.indexOf('## Configs'))
    assert.match(asyncRef, /^### `AsyncClient\.capture`$/m)
    assert.doesNotMatch(asyncRef, /^### Capture$/m)
})

test('types explain members and skip empty stubs', () => {
    assert.match(md, /^### `FeatureFlagEvaluations`\n\nA snapshot of flag values\./m)
    assert.match(md, /^#### `FeatureFlagEvaluations\.is_enabled`$/m)
    assert.doesNotMatch(md, /`Empty`/)
    assert.match(md, /^\| `GZIP`<br \/>\*\*Value:\*\* `"gzip"`<br \/>gzip, from the standard library\. \|$/m)
})

test('guides drop their H1 and nest under their section', () => {
    assert.match(md, /## Getting started\n\nInstall it\.\n\n### Install/)
    assert.doesNotMatch(md, /^# /m)
})

test('configs render as headed settings and the page has no reverse usage lists', () => {
    assert.match(md, /^## Configs\n\n<details>\n<summary>Settings \(1\)<\/summary>\n\n\| Setting \|\n\| --- \|\n\| `POSTHOG_MW_CAPTURE_EXCEPTIONS`<br \/>\*\*Where:\*\* Django settings · \*\*Type:\*\* `bool` · \*\*Default:\*\* `True`<br \/>Captures exceptions\. \|$/m)
    assert.doesNotMatch(md, /Used by/i)
})

test('front matter names the SDK and version', () => {
    assert.match(md, /^---\ntitle: PostHog Python SDK\nsdk: python\nversion: 8\.0\.0\nmajor: 8\nstatus: experimental\n---\n/)
    assert.throws(() => render({ ...page, status: 'preview' }, capabilities), /status/)
})

test('migration guides render as their own page, each guide an H2', () => {
    const out = renderMigration(page)
    assert.match(out, /^---\ntitle: PostHog Python SDK migration guides\nsdk: python\nkind: migration\n---\n\n## Migrating from 7\.x to 8\.0\n\n### Capture\n\nIt changed\.\n\n## Migrating from 6\.x to 7\.0\n\nOlder\.\n$/)
    assert.doesNotMatch(md, /Migrating from/)
    assert.equal(renderMigration({ ...page, migrations: [] }), null)
})
