// Smart Ledger - 最小限のフロントエンド補助スクリプト(フレームワーク不使用)
(function () {
  // ---- 内訳(allocations)エディタ ----
  var form = document.getElementById('alloc-form');
  if (form) {
    var total = parseInt(form.dataset.total || '0', 10);
    var rows = document.getElementById('alloc-rows');
    var sumEl = document.getElementById('alloc-sum');

    function fmt(n) { return n.toLocaleString('ja-JP'); }

    // 入力中の内訳を集計する。金額欄が空でカテゴリかメモのある行は「空の行」(保存時にサーバーが残額を入れる。2 行以上はエラー)
    function tally() {
      var sum = 0, filled = 0, blanks = [];
      rows.querySelectorAll('.alloc-row').forEach(function (row) {
        var amount = row.querySelector('.alloc-amount');
        var category = row.querySelector('select').value;
        var memo = row.querySelector('input[type=text]');
        var raw = (amount.value || '').replace(/,/g, '').trim();
        var v = parseInt(raw, 10);
        if (!isNaN(v)) { sum += v; filled += 1; } else if (!raw && (category || (memo && memo.value.trim()))) { blanks.push(category); }
      });
      return { sum: sum, filled: filled, blanks: blanks };
    }

    // 残額が 0 円か明細金額と逆の符号になるか(サーバーの allocations.flips_sign と同じ判定。保存時にエラーになる)
    function flipsSign(amount) { return amount === 0 || (amount < 0) !== (total < 0); }

    function recalc() {
      var t = tally();
      var diff = total - t.sum;
      var text = '合計 ' + fmt(t.sum) + ' 円 / ' + fmt(total) + ' 円';
      var state = '';  // match / mismatch / ''(未入力)
      if (t.blanks.length === 1) {
        var bad = flipsSign(diff);
        text += '(残り ' + fmt(diff) + ' 円 → ' + (t.blanks[0] || 'カテゴリ未選択') + (bad ? '。0 円や明細金額と逆の符号の残額は入れられません' : '') + ')';
        state = bad || !t.blanks[0] ? 'mismatch' : 'match';
      } else if (t.blanks.length > 1) {
        text += '(金額が空の行が ' + t.blanks.length + ' 行あります。残額を入れられるのは 1 行だけです)';
        state = 'mismatch';
      } else if (t.filled > 0) {
        if (diff !== 0) text += '(差額 ' + fmt(diff) + ' 円)';
        state = diff === 0 ? 'match' : 'mismatch';
      }
      sumEl.textContent = text;
      sumEl.classList.toggle('mismatch', state === 'mismatch');
      sumEl.classList.toggle('match', state === 'match');
    }

    function bindRow(row) {
      var remove = row.querySelector('.alloc-remove');
      if (remove) remove.addEventListener('click', function () {
        if (rows.children.length > 1) { row.remove(); } else {
          row.querySelectorAll('input').forEach(function (i) { i.value = ''; });
          row.querySelector('select').value = '';
        }
        recalc();
      });
    }

    // 金額・カテゴリ・メモのどれが変わっても表示を更新する(追加した行も対象にするため行の親で受ける)
    rows.addEventListener('input', recalc);
    rows.addEventListener('change', recalc);
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

    // 合計の不一致で送信を止める(後で登録する data-submit-once の共通ハンドラは defaultPrevented を見て何もしない)。
    // 空の行が 1 行なら残額はサーバーが入れるので止めない(0 円・逆符号・カテゴリ未選択はサーバーがエラーにする)
    form.addEventListener('submit', function (e) {
      var t = tally();
      if (t.blanks.length > 1) {
        e.preventDefault();
        alert('金額が空の行が ' + t.blanks.length + ' 行あります。残額を入れられるのは 1 行だけです。');
      } else if (t.blanks.length === 0 && t.filled > 0 && t.sum !== total) {
        e.preventDefault();
        alert('内訳の合計(' + fmt(t.sum) + ' 円)が明細金額(' + fmt(total) + ' 円)と一致しません。');
      }
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

  // ---- data-confirm / data-submit-once 属性を持つフォームの送信確認と二重送信防止 ----
  // 文言に加盟店名・ファイル名などユーザー由来の文字列を含めても、属性値は HTML エスケープされるため
  // インラインの onsubmit="confirm('...')" と違って JS 文字列が壊れない。
  // data-submit-once だけのフォーム(手動明細の追加・内訳・取込確定など)は確認せず、二重送信の防止だけを行う。
  // 送信ボタンに data-busy-label があれば、無効化と同時に表示をその文言に替える
  document.querySelectorAll('form[data-confirm], form[data-submit-once]').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (e.defaultPrevented) return;  // 先に登録した検証(内訳の合計など)で止まった送信
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { e.preventDefault(); return; }
      // 送信データの組み立て後に無効化する(同期で disabled にすると name 付きボタンの値が送られない)
      setTimeout(function () {
        form.querySelectorAll('button[type="submit"]').forEach(function (btn) {
          btn.disabled = true;
          btn.dataset.submitting = '1';
          if (btn.dataset.busyLabel) { btn.dataset.idleLabel = btn.textContent; btn.textContent = btn.dataset.busyLabel; }
        });
      }, 0);
    });
  });

  // 戻るで bfcache から復元されたページでは、上で無効化したままの送信ボタンを元に戻す(サーバー側の disabled は触らない)
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('button[data-submitting]').forEach(function (btn) {
      btn.disabled = false;
      delete btn.dataset.submitting;
      if (btn.dataset.idleLabel) btn.textContent = btn.dataset.idleLabel;
    });
  });
})();
