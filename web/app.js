// GoodNotes Splitter Web App Controller

(function () {
  'use strict';

  const dropzone = document.getElementById('dropzone');
  const fileInput = document.getElementById('fileInput');
  const targetMbInput = document.getElementById('targetMb');
  const statusPanel = document.getElementById('statusPanel');
  const progressBar = document.getElementById('progressBar');
  const statusText = document.getElementById('statusText');
  const resultCard = document.getElementById('resultCard');
  const metaList = document.getElementById('metaList');
  const resultTableBody = document.getElementById('resultTableBody');
  const btnShowLog = document.getElementById('btnShowLog');
  const btnDownloadAll = document.getElementById('btnDownloadAll');
  const logBox = document.getElementById('logBox');

  let currentParts = [];
  let currentSourceFileName = '';
  let logs = [];

  function log(msg) {
    const timestamp = new Date().toLocaleTimeString();
    const line = `[${timestamp}] ${msg}`;
    logs.push(line);
    logBox.textContent = logs.join('\n');
    logBox.scrollTop = logBox.scrollHeight;
  }

  function setProgress(percent, text) {
    statusPanel.style.display = 'block';
    progressBar.style.width = `${Math.min(100, Math.max(0, percent))}%`;
    if (text) {
      statusText.textContent = text;
      log(text);
    }
  }

  function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  }

  // Drag and Drop
  dropzone.addEventListener('click', () => fileInput.click());

  dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('dragover');
  });

  dropzone.addEventListener('dragleave', () => {
    dropzone.classList.remove('dragover');
  });

  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleFile(e.dataTransfer.files[0]);
    }
  });

  fileInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length > 0) {
      handleFile(e.target.files[0]);
    }
  });

  btnShowLog.addEventListener('click', () => {
    logBox.style.display = logBox.style.display === 'none' ? 'block' : 'none';
  });

  btnDownloadAll.addEventListener('click', async () => {
    if (currentParts.length === 0) return;
    btnDownloadAll.disabled = true;
    const oldText = btnDownloadAll.textContent;
    btnDownloadAll.textContent = 'ZIP圧縮中...';

    try {
      const allZip = new JSZip();
      for (const p of currentParts) {
        allZip.file(p.filename, p.data);
      }
      const blob = await allZip.generateAsync({ type: 'blob', compression: 'STORE' });
      const baseName = currentSourceFileName.replace(/\.goodnotes$/i, '');
      const downloadName = `${baseName}_parts.zip`;
      downloadBlob(blob, downloadName);
    } catch (e) {
      alert('一括ZIP作成に失敗しました: ' + e.message);
    } finally {
      btnDownloadAll.disabled = false;
      btnDownloadAll.textContent = oldText;
    }
  });

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 1000);
  }

  async function handleFile(file) {
    if (!file.name.toLowerCase().endsWith('.goodnotes')) {
      alert('.goodnotes 形式のファイルを選択してください。');
      return;
    }

    currentSourceFileName = file.name;
    logs = [];
    currentParts = [];
    resultCard.style.display = 'none';
    resultTableBody.innerHTML = '';
    metaList.innerHTML = '';

    setProgress(5, `ファイル読み込み中: ${file.name} (${formatBytes(file.size)})`);

    try {
      const arrayBuffer = await file.arrayBuffer();
      setProgress(20, 'ZIPアーカイブ解凍中...');
      const zip = await JSZip.loadAsync(arrayBuffer);

      setProgress(40, 'ドキュメント構造・イベント解析中...');
      const doc = await SplitterEngine.parseGoodNotes(zip);

      log(`解析完了: 有効ページ数=${doc.pages.length}、イベント総数=${doc.events.length}`);

      if (doc.pages.length === 0) {
        throw new Error('有効なページが見つかりませんでした。');
      }

      const targetMb = parseFloat(targetMbInput.value) || 4.5;
      const targetBytes = targetMb * 1024 * 1024;
      setProgress(60, `分割計画策定中 (目標: ${targetMb} MB)...`);
      const pageGroups = SplitterEngine.planPartitions(doc, targetBytes);

      log(`分割計画: 全${doc.pages.length}ページを ${pageGroups.length} パートに分割`);

      currentParts = [];
      const baseName = file.name.replace(/\.goodnotes$/i, '');

      for (let i = 0; i < pageGroups.length; i++) {
        const pNum = i + 1;
        const progress = 60 + Math.floor(((i + 1) / pageGroups.length) * 35);
        setProgress(progress, `パート ${pNum} / ${pageGroups.length} を生成中...`);

        const partRes = await SplitterEngine.buildPartZip(doc, pageGroups[i], pNum, JSZip);
        const outName = `${baseName}_part${String(pNum).padStart(3, '0')}.goodnotes`;
        const blob = new Blob([partRes.data], { type: 'application/octet-stream' });

        currentParts.push({
          partIndex: pNum,
          filename: outName,
          blob: blob,
          data: partRes.data,
          pageCount: partRes.pageCount,
          startPage: partRes.startPage,
          endPage: partRes.endPage,
          bytes: partRes.bytes,
        });

        log(`パート ${pNum} 出力完了: ${outName} (${formatBytes(partRes.bytes)}, ページ: ${partRes.startPage}〜${partRes.endPage})`);
      }

      setProgress(100, 'すべての分割処理が完了しました。');
      renderResults(doc, file);
    } catch (err) {
      console.error(err);
      setProgress(0, `エラー: ${err.message}`);
      alert(`処理中にエラーが発生しました: ${err.message}`);
    }
  }

  function renderResults(doc, originalFile) {
    resultCard.style.display = 'block';

    metaList.innerHTML = `
      <div class="meta-item">
        <div class="meta-title">元のファイル名</div>
        <div class="meta-val" style="word-break: break-all;">${originalFile.name}</div>
      </div>
      <div class="meta-item">
        <div class="meta-title">元のファイルサイズ</div>
        <div class="meta-val">${formatBytes(originalFile.size)} (${originalFile.size.toLocaleString()} B)</div>
      </div>
      <div class="meta-item">
        <div class="meta-title">有効ページ総数</div>
        <div class="meta-val">${doc.pages.length} ページ</div>
      </div>
      <div class="meta-item">
        <div class="meta-title">生成パート数</div>
        <div class="meta-val">${currentParts.length} ファイル</div>
      </div>
    `;

    resultTableBody.innerHTML = '';
    for (const part of currentParts) {
      const tr = document.createElement('tr');
      const isUnder5Mb = part.bytes < 5 * 1024 * 1024;
      const rangeText = part.startPage === part.endPage ? `P.${part.startPage}` : `P.${part.startPage} 〜 P.${part.endPage}`;

      tr.innerHTML = `
        <td style="font-weight: 600;">${part.filename}</td>
        <td>${rangeText}</td>
        <td>${part.pageCount} ページ</td>
        <td>${formatBytes(part.bytes)} (${part.bytes.toLocaleString()} B)</td>
        <td><span class="badge-ok">${isUnder5Mb ? '5MB以内' : '容量超過'}</span></td>
        <td>
          <button class="btn btn-secondary btn-sm btn-download-part" data-part="${part.partIndex}">ダウンロード</button>
        </td>
      `;
      resultTableBody.appendChild(tr);
    }

    // Attach click handlers to part download buttons
    const downloadBtns = resultTableBody.querySelectorAll('.btn-download-part');
    downloadBtns.forEach((btn) => {
      btn.addEventListener('click', (e) => {
        const pIndex = parseInt(e.target.getAttribute('data-part'), 10);
        const part = currentParts.find((p) => p.partIndex === pIndex);
        if (part) {
          downloadBlob(part.blob, part.filename);
        }
      });
    });
  }
})();
