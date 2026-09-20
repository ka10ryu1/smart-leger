// Smart Ledger - 最小限のフロントエンド補助スクリプト(フレームワーク不使用)
(function () {
  // ---- 内訳(allocations)エディタ ----
  var form = document.getElementById('alloc-form');
  if (form) {
    var total = parseInt(form.dataset.total || '0', 10);
    var rows = document.getElementById('alloc-rows');
    var sumEl = document.getElementById('alloc-sum');
    var submit = document.getElementById('alloc-submit');

    function fmt(n) { return n.toLocaleString('ja-JP'); }

    function recalc() {
      var sum = 0, filled = 0;
      rows.querySelectorAll('.alloc-amount').forEach(function (input) {
        var v = parseInt((input.value || '').replace(/,/g, ''), 10);
        if (!isNaN(v)) { sum += v; filled += 1; }
      });
      var diff = total - sum;
      sumEl.textContent = '合計 ' + fmt(sum) + ' 円 / ' + fmt(total) + ' 円' + (diff !== 0 && filled ? '(差額 ' + fmt(diff) + ' 円)' : '');
      sumEl.classList.toggle('mismatch', filled > 0 && diff !== 0);
      sumEl.classList.toggle('match', filled > 0 && diff === 0);
    }

    function bindRow(row) {
      var amount = row.querySelector('.alloc-amount');
      if (amount) amount.addEventListener('input', recalc);
      var remove = row.querySelector('.alloc-remove');
      if (remove) remove.addEventListener('click', function () {
        if (rows.children.length > 1) { row.remove(); } else {
          row.querySelectorAll('input').forEach(function (i) { i.value = ''; });
          row.querySelector('select').value = '';
        }
        recalc();
      });
    }

    rows.querySelectorAll('.alloc-row').forEach(bindRow);
    document.getElementById('alloc-add').addEventListener('click', function () {
      var template = rows.querySelector('.alloc-row');
      var clone = template.cloneNode(true);
      clone.querySelectorAll('input').forEach(function (i) { i.value = ''; });
      clone.querySelector('select').value = '';
      rows.appendChild(clone);
      bindRow(clone);
      clone.querySelector('select').focus();
    });

    // 差額入力の補助: 空の金額欄をダブルクリックで残額を入れる
    rows.addEventListener('dblclick', function (e) {
      if (e.target.classList.contains('alloc-amount') && !e.target.value) {
        var sum = 0;
        rows.querySelectorAll('.alloc-amount').forEach(function (i) { var v = parseInt(i.value, 10); if (!isNaN(v)) sum += v; });
        e.target.value = total - sum;
        recalc();
      }
    });

    form.addEventListener('submit', function (e) {
      var sum = 0, filled = 0;
      rows.querySelectorAll('.alloc-amount').forEach(function (i) { var v = parseInt(i.value, 10); if (!isNaN(v)) { sum += v; filled += 1; } });
      if (filled > 0 && sum !== total) {
        e.preventDefault();
        alert('内訳の合計(' + fmt(sum) + ' 円)が明細金額(' + fmt(total) + ' 円)と一致しません。');
        return false;
      }
      submit.disabled = true;
    });
    recalc();
  }

  // ---- ルールパターンの入力に合わせて「一致する明細が他に N 件」を更新 ----
  var patternInput = document.getElementById('rule-pattern');
  var previewCount = document.getElementById('rule-preview-count');
  if (patternInput && previewCount) {
    var timer = null;
    patternInput.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        fetch(previewCount.dataset.url + '?pattern=' + encodeURIComponent(patternInput.value))
          .then(function (res) { return res.ok ? res.json() : null; })
          .then(function (json) { if (json) previewCount.textContent = json.count; })
          .catch(function () { /* 表示用なので失敗は無視 */ });
      }, 300);
    });
  }

  // ---- data-confirm 属性を持つフォームの送信確認 ----
  // 文言に加盟店名・ファイル名などユーザー由来の文字列を含めても、属性値は HTML エスケープされるため
  // インラインの onsubmit="confirm('...')" と違って JS 文字列が壊れない
  document.querySelectorAll('form[data-confirm]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (!window.confirm(form.dataset.confirm)) { e.preventDefault(); }
    });
  });

  // ---- 取込確定ボタンの二重送信防止 ----
  var commitForm = document.getElementById('commit-form');
  if (commitForm) {
    commitForm.addEventListener('submit', function () {
      var btn = document.getElementById('commit-btn');
      btn.disabled = true;
      btn.textContent = '分類・保存中…';
    });
  }
})();
