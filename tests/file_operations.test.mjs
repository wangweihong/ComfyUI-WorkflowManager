import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { resolve, dirname } from 'node:path';

// Load the real browser modules with ComfyUI's two host imports stubbed.
async function harness(respond = () => ({ success: true })) {
    const calls = [];
    const elements = new Map();
    const document = {
        querySelector: selector => elements.get(selector) || null,
        querySelectorAll: () => [],
        createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, remove() {} }),
        body: { appendChild() {}, contains: () => false },
    };
    const context = vm.createContext({ console, document, CSS: { escape: x => x },
        window: { dispatchEvent() {} }, setTimeout: () => 0, clearTimeout() {} });
    const cache = new Map();
    const hostApi = new vm.SyntheticModule(['api'], function () {
        this.setExport('api', { fetchApi: async (url, options) => {
            const call = { url, data: options?.body ? JSON.parse(options.body) : undefined };
            calls.push(call);
            return { json: async () => respond(call) };
        } });
    }, { context });
    const hostApp = new vm.SyntheticModule(['app'], function () {
        this.setExport('app', { extensionManager: { dialog: {
            prompt: async () => '新名称', confirm: async () => true
        } } });
    }, { context });
    async function load(path) {
        if (cache.has(path)) return cache.get(path);
        const module = new vm.SourceTextModule(await readFile(path, 'utf8'), { context, identifier: path });
        cache.set(path, module);
        await module.link((specifier, parent) => {
            if (specifier.endsWith('/scripts/api.js')) return hostApi;
            if (specifier.endsWith('/scripts/app.js')) return hostApp;
            return load(resolve(dirname(parent.identifier), specifier));
        });
        return module;
    }
    const operations = await load(resolve('js/workflow_operations.js'));
    await operations.evaluate();
    const state = cache.get(resolve('js/workflow_state.js')).namespace.managerState;
    return { operations: operations.namespace, state, calls, elements };
}

function transfer() {
    const values = new Map();
    return { types: [], items: [], files: [], setData: (k, v) => values.set(k, v),
        getData: k => values.get(k) || '', setDragImage() {} };
}
function event(row, dataTransfer) {
    return { target: { closest: selector => selector === '.file-item' ? row : null },
        currentTarget: {}, dataTransfer, preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {} };
}

test('internal drag/drop moves a Markdown file into a folder', async () => {
    const { operations, calls } = await harness();
    const data = transfer();
    const source = { dataset: { path: '说明.md', name: '说明.md', type: 'file' }, classList: { add() {} } };
    operations.handleDragStart(event(source, data));
    const folder = { dataset: { path: '目标目录', type: 'directory' } };
    operations.handleDrop(event(folder, data));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/workflow-manager/move');
    assert.equal(calls[0].data.source_path, '说明.md');
    assert.equal(calls[0].data.target_dir, '目标目录');
});

test('dragging a folder and its selected child handles the tree once', async () => {
    const { operations, state, calls } = await harness();
    state.selectedItems.add('父目录');
    state.selectedItems.add('父目录/说明.md');
    const source = { dataset: { path: '父目录', name: '父目录', type: 'directory' }, classList: { add() {} } };
    const data = transfer();
    operations.handleDragStart(event(source, data));
    const folder = { dataset: { path: '目标', type: 'directory' } };
    operations.handleDrop(event(folder, data));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.length, 1);
    assert.equal(calls[0].data.source_path, '父目录');
});

test('copy operation preserves mixed JSON, text, and binary selections', async () => {
    const { operations, calls } = await harness();
    await operations.performDropOperation(['flow.JSON', 'notes.txt', 'photo.png'], '目标', true);
    assert.deepEqual(calls.map(call => call.data.source_path), ['flow.JSON', 'notes.txt', 'photo.png']);
    assert.ok(calls.every(call => call.url === '/workflow-manager/copy'));
});


test('all-files mode operates on explicitly selected covers without implicit duplicates', async () => {
    const { operations, state, calls } = await harness();
    state.fileMode = 'all';
    await operations.performDropOperation(['flow.json', 'flow.png'], '目标', false);
    assert.equal(calls.length, 2);
    assert.ok(calls.every(call => call.data.sync_preview === false));
});


test('destination move supports root and removes implicit parent/child duplicates', async () => {
    const { operations, calls, state } = await harness();
    state.expandedFolders.add('旧目录/父目录');
    state.expandedFolders.add('旧目录/父目录/子目录');
    const result = await operations.moveItemsTo(['旧目录/父目录', '旧目录/父目录/子目录/a.json', '旧目录/b.JSON'], '');
    assert.deepEqual(calls.map(call => call.data.source_path), ['旧目录/父目录', '旧目录/b.JSON']);
    assert.ok(calls.every(call => call.data.target_dir === ''));
    assert.equal(result.moved.length, 2);
    assert.equal(state.expandedFolders.size, 0);
});

test('destination move skips files already in target and refuses self/descendant targets', async () => {
    const { operations, calls } = await harness();
    const unchanged = await operations.moveItemsTo(['目录/a.json'], '目录');
    assert.equal(unchanged.skipped.length, 1);
    const forbidden = await operations.moveItemsTo(['目录'], '目录/子目录');
    assert.equal(forbidden.failed.length, 1);
    assert.equal(calls.length, 0);
});

test('partial move reports conflict paths without retrying successes and refreshes the list', async () => {
    const { operations, calls } = await harness(call => call.data.source_path === '冲突.json'
        ? { success: false, error: '目标位置已存在同名项目' } : { success: true });
    let refreshes = 0;
    operations.setLoadDirectoryRef(async () => { refreshes++; });
    const result = await operations.moveItemsTo(['成功.json', '冲突.json'], '目标');
    assert.deepEqual(Array.from(result.moved), ['成功.json']);
    assert.equal(result.failed[0].path, '冲突.json');
    assert.match(result.failed[0].error, /同名/);
    assert.equal(calls.length, 2);
    assert.equal(refreshes, 1);
});
