import { api } from '../../../scripts/api.js';
import { formatFileSize, formatDate, showToast } from './workflow_state.js';

// Never render file contents as HTML, including Markdown and HTML source files.
export async function previewFile(path) {
    try {
        const response = await api.fetchApi(`/workflow-manager/read-file?path=${encodeURIComponent(path)}`);
        const result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.error || '无法读取文件');
        document.querySelector('#workflow-file-preview')?.close();
        const previousFocus = document.activeElement;
        const dialog = document.createElement('dialog');
        dialog.id = 'workflow-file-preview';
        dialog.setAttribute('aria-label', '文件预览');
        dialog.style.cssText = 'width:min(960px,90vw);max-height:85vh;padding:20px;border:1px solid var(--border-color,#555);border-radius:10px;background:var(--comfy-menu-bg,#222);color:var(--input-text,#eee);';
        dialog.innerHTML = `<div style="display:flex;align-items:center;gap:12px"><h3 style="flex:1;min-width:0;overflow-wrap:anywhere;margin:0"></h3><button type="button" class="preview-copy">复制文本</button><button type="button" class="preview-close" aria-label="关闭预览">关闭</button></div><p class="preview-meta" style="font-size:12px;overflow-wrap:anywhere"></p><p class="preview-notice"></p><pre tabindex="0" style="max-height:58vh;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;background:var(--comfy-input-bg,#171717);user-select:text"></pre>`;
        for (const button of dialog.querySelectorAll('button')) {
            button.style.cssText = 'padding:6px 12px;border:1px solid var(--border-color,#555);border-radius:5px;background:var(--comfy-input-bg,#333);color:inherit;cursor:pointer;white-space:nowrap';
        }
        dialog.querySelector('h3').textContent = result.name;
        dialog.querySelector('.preview-meta').textContent = `${path} · ${result.mime} · ${formatFileSize(result.size)} · ${formatDate(result.modified)}${result.encoding ? ` · ${result.encoding}` : ''}`;
        dialog.querySelector('.preview-notice').textContent = result.kind === 'binary'
            ? '此文件为二进制文件或不支持的文本编码，暂不提供内容预览。'
            : result.truncated ? '文件较大，仅预览前 1 MiB；复制也仅包含预览部分。' : '只读预览';
        dialog.querySelector('pre').textContent = result.content ?? '';
        dialog.querySelector('pre').hidden = result.kind !== 'text';
        const copyButton = dialog.querySelector('.preview-copy');
        copyButton.hidden = result.kind !== 'text';
        copyButton.onclick = async () => {
            try {
                if (navigator.clipboard?.writeText) {
                    await navigator.clipboard.writeText(result.content);
                } else {
                    const input = document.createElement('textarea');
                    input.value = result.content;
                    dialog.appendChild(input);
                    input.select();
                    const copied = document.execCommand('copy');
                    input.remove();
                    if (!copied) throw new Error('请选中文本后复制');
                }
                copyButton.textContent = '已复制';
            } catch (error) {
                showToast(`复制失败：${error.message}`, 'error');
            }
        };
        dialog.querySelector('.preview-close').onclick = () => dialog.close();
        dialog.addEventListener('keydown', event => event.stopPropagation());
        dialog.addEventListener('close', () => {
            dialog.remove();
            if (previousFocus?.isConnected) previousFocus.focus();
        }, { once: true });
        document.body.appendChild(dialog);
        dialog.showModal();
        dialog.querySelector('.preview-close').focus();
    } catch (error) {
        showToast(`预览失败：${error.message}`, 'error');
    }
}
