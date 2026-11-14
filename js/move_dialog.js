// Directory selection is independent from the manager's current folder/selection.
export async function showMoveDialog({ paths, initialPath, browse, move }) {
    if (!paths.length || document.querySelector('#workflow-move-dialog')) return;
    const dialog = document.createElement('dialog');
    dialog.id = 'workflow-move-dialog';
    dialog.setAttribute('aria-label', '移动到其他目录');
    dialog.style.cssText = 'width:min(640px,90vw);max-height:85vh;padding:20px;border:1px solid var(--border-color,#555);border-radius:10px;background:var(--comfy-menu-bg,#222);color:var(--input-text,#eee)';
    dialog.innerHTML = `<h3 style="margin:0 0 12px">移动到其他目录</h3>
        <p class="move-summary" style="max-height:14vh;overflow:auto;overflow-wrap:anywhere"></p>
        <div style="display:flex;gap:8px;margin-bottom:10px">
            <button type="button" class="move-root">根目录</button>
            <button type="button" class="move-parent">上一级</button>
        </div>
        <p class="move-location" style="overflow-wrap:anywhere"></p>
        <div class="move-folders" role="group" aria-label="目标目录中的子文件夹" style="max-height:38vh;min-height:0;overflow-y:auto;overflow-x:hidden;display:flex;flex-direction:column;gap:6px;padding:8px 0"></div>
        <p class="move-feedback" role="status" style="white-space:pre-wrap;overflow-wrap:anywhere"></p>
        <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
            <button type="button" class="move-cancel">取消</button>
            <button type="button" class="move-confirm" disabled>移动到此目录</button>
        </div>`;
    const buttonStyle = 'padding:7px 12px;border:1px solid var(--border-color,#555);border-radius:5px;background:var(--comfy-input-bg,#333);color:inherit;cursor:pointer;white-space:nowrap';
    for (const button of dialog.querySelectorAll('button')) button.style.cssText = buttonStyle;
    const confirm = dialog.querySelector('.move-confirm');
    const parent = dialog.querySelector('.move-parent');
    const root = dialog.querySelector('.move-root');
    const cancel = dialog.querySelector('.move-cancel');
    const folders = dialog.querySelector('.move-folders');
    const feedback = dialog.querySelector('.move-feedback');
    let pending = [...paths];
    let directory = initialPath;
    let busy = false;
    let ready = false;
    let requestVersion = 0;
    const previousFocus = document.activeElement;
    const parentPath = path => path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
    const forbidden = path => pending.some(source => path === source || path.startsWith(source + '/'));
    function update() {
        dialog.querySelector('.move-summary').textContent = `已选择 ${pending.length} 项：${pending.map(path => path.split('/').pop()).join('、')}`;
        dialog.querySelector('.move-location').textContent = `目标：${directory ? '根目录 / ' + directory : '根目录'}`;
        confirm.disabled = busy || !ready || forbidden(directory) || pending.every(path => parentPath(path) === directory);
        parent.disabled = busy || !directory;
        root.disabled = busy || (!directory && ready);
        cancel.disabled = busy;
    }
    async function navigate(path) {
        if (busy) return;
        const version = ++requestVersion;
        ready = false;
        update();
        folders.textContent = '正在读取目录…';
        feedback.textContent = '';
        try {
            const result = await browse(path);
            if (version !== requestVersion || !dialog.open) return;
            if (!result.success) throw new Error(result.error || '无法读取目录');
            directory = path;
            folders.replaceChildren();
            const directories = result.items.filter(item => item.type === 'directory');
            for (const item of directories) {
                const button = document.createElement('button');
                button.type = 'button';
                button.dataset.targetPath = item.path;
                // Rows must not shrink to fit the scroll container: many folders
                // otherwise collapse the content box beneath the button padding.
                button.style.cssText = buttonStyle + ';display:flex;align-items:flex-start;gap:8px;flex:0 0 auto;box-sizing:border-box;width:100%;min-width:0;min-height:40px;height:auto;text-align:left;white-space:normal;line-height:1.5';
                const icon = document.createElement('span');
                icon.textContent = '📁';
                icon.setAttribute('aria-hidden', 'true');
                icon.style.cssText = 'flex:0 0 auto;line-height:inherit';
                const name = document.createElement('span');
                name.textContent = item.name;
                name.style.cssText = 'flex:1 1 auto;min-width:0;white-space:normal;overflow-wrap:anywhere;line-height:inherit';
                button.append(icon, name);
                button.title = item.name;
                button.disabled = forbidden(item.path);
                if (button.disabled) button.title = '不能移动到自身或其子目录';
                button.onclick = () => navigate(item.path);
                folders.appendChild(button);
            }
            if (!directories.length) folders.textContent = '没有子文件夹，可以移动到当前目录。';
            ready = true;
        } catch (error) {
            if (version !== requestVersion || !dialog.open) return;
            folders.textContent = '无法读取子文件夹，请返回上一级或根目录重试。';
            feedback.textContent = error.message;
        }
        update();
    }
    root.onclick = () => navigate('');
    parent.onclick = () => navigate(parentPath(directory));
    cancel.onclick = () => dialog.close();
    confirm.onclick = async () => {
        if (confirm.disabled) return;
        busy = true;
        update();
        for (const button of folders.querySelectorAll('button')) button.disabled = true;
        feedback.textContent = '正在移动…';
        try {
            const result = await move(pending, directory);
            if (!result.failed.length) {
                dialog.close();
                return;
            }
            pending = result.failed.map(item => item.path);
            feedback.textContent = `已移动 ${result.moved.length} 项，失败 ${result.failed.length} 项。\n` + result.failed.map(item => `${item.path}：${item.error}`).join('\n');
        } catch (error) {
            feedback.textContent = `移动失败：${error.message}`;
        } finally {
            busy = false;
            update();
            // Re-enable directory navigation after partial failures.
            for (const button of folders.querySelectorAll('button')) button.disabled = forbidden(button.dataset.targetPath);
        }
    };
    dialog.addEventListener('keydown', event => event.stopPropagation());
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => {
        requestVersion++;
        dialog.remove();
        if (previousFocus?.isConnected) previousFocus.focus();
    }, { once: true });
    document.body.appendChild(dialog);
    dialog.showModal();
    cancel.focus();
    await navigate(initialPath);
}
