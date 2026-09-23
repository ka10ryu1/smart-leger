"""Flask test client による画面フローのテスト"""

from __future__ import annotations

import io
from datetime import date

import pytest
from flask.testing import FlaskClient

from smart_ledger import create_app
from smart_ledger.config import Config
from smart_ledger.models import Allocation, LedgerData, Transaction
from smart_ledger.services.excel_repository import ExcelLockedError


@pytest.fixture
def client(test_config: Config) -> FlaskClient:
    """テスト用アプリのクライアント

    Args:
        test_config: 一時ディレクトリを使う設定
    """
    app = create_app(test_config)
    app.config['TESTING'] = True
    return app.test_client()


def upload(client: FlaskClient, content: bytes, filename: str = 'test.csv') -> str:
    """CSV をアップロードしてプレビュー HTML を返す

    Args:
        client: テストクライアント
        content: CSV のバイト列
        filename: ファイル名
    """
    response = client.post(
        '/import/preview', data={'csv_file': (io.BytesIO(content), filename)}, content_type='multipart/form-data'
    )
    assert response.status_code == 200
    return response.get_data(as_text=True)


def import_csv(client: FlaskClient, content: bytes, filename: str = 'test.csv') -> str:
    """CSV をプレビュー → 確定まで取り込み、確定後の HTML を返す

    Args:
        client: テストクライアント
        content: CSV のバイト列
        filename: ファイル名
    """
    token = upload(client, content, filename).split('name="token" value="')[1].split('"')[0]
    return client.post('/import/commit', data={'token': token}, follow_redirects=True).get_data(as_text=True)


def first_tx_id(client: FlaskClient, query: str) -> str:
    """加盟店名で検索した先頭の明細 ID を返す

    Args:
        client: テストクライアント
        query: 明細一覧の検索語
    """
    return client.get(f'/transactions?q={query}').get_data(as_text=True).split('/transactions/')[1].split('/edit')[0]


def test_pages_render(client: FlaskClient) -> None:
    """データが無い状態でも各画面が 200 を返す

    Args:
        client: テストクライアント
    """
    for path in ('/', '/annual', '/transactions', '/review', '/rules', '/categories', '/import', '/health'):
        assert client.get(path).status_code == 200

    assert client.get('/health').get_json()['app'] == 'smart-ledger'  # app.py の二重起動判定が照合する


def test_import_preview_rejects_missing_empty_and_malformed_files(client: FlaskClient) -> None:
    """ファイル未選択・空ファイル・必要ヘッダー無しは保存せずエラー表示する

    Args:
        client: テストクライアント
    """
    missing = client.post('/import/preview', data={}, follow_redirects=True).get_data(as_text=True)
    assert 'CSV ファイルを選択してください' in missing

    empty = client.post(
        '/import/preview',
        data={'csv_file': (io.BytesIO(b''), 'empty.csv')},
        content_type='multipart/form-data',
        follow_redirects=True,
    ).get_data(as_text=True)
    assert '空のファイルです' in empty

    malformed = client.post(
        '/import/preview',
        data={'csv_file': (io.BytesIO(b'a,b\r\n1,2\r\n'), 'bad.csv')},
        content_type='multipart/form-data',
        follow_redirects=True,
    ).get_data(as_text=True)
    assert 'CSV を解析できませんでした' in malformed


def test_import_commit_rejects_unknown_preview_token(client: FlaskClient) -> None:
    """存在しないプレビュートークンは再選択を促し、データを変更しない

    Args:
        client: テストクライアント
    """
    body = client.post('/import/commit', data={'token': 'unknown'}, follow_redirects=True).get_data(as_text=True)
    assert 'プレビュー情報が見つかりません' in body


def test_excel_locked_error_renders_423(client: FlaskClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Excel を読み込めないときは利用者向けの423エラー画面を返す

    Args:
        client: テストクライアント
        monkeypatch: リポジトリ読み込みをロックエラーにする
    """
    repo = client.application.extensions['smart_ledger'].repo
    monkeypatch.setattr(repo, 'load', lambda: (_ for _ in ()).throw(ExcelLockedError('Excel が開かれています')))
    response = client.get('/')
    assert response.status_code == 423
    assert 'Excel を書き込めません' in response.get_data(as_text=True)


def test_web_flow(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """取込 → ダッシュボード → カテゴリ変更（ルール登録） → 内訳 → 再取込拒否 → 取り消し → 再取込 の一連のフロー

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    html = upload(client, fixture_csv_bytes)
    assert '10 件を取り込む' in html
    assert 'テストデンリヨク 8ガツブン' in html
    token = html.split('name="token" value="')[1].split('"')[0]
    assert client.get('/transactions').get_data(as_text=True).count('編集') == 0  # プレビューでは保存されない

    body = client.post('/import/commit', data={'token': token}, follow_redirects=True).get_data(as_text=True)
    assert '10 件を取り込みました' in body

    # 8月 総支出 = 7850+3240+10000+5000+5000+10000+4500 = 45590
    assert '45,590' in client.get('/?month=2026-08').get_data(as_text=True)

    tx_id = first_tx_id(client, 'SAMPLE')
    response = client.post(
        f'/transactions/{tx_id}/category',
        data={'category': 'その他', 'scope': 'always', 'memo': 'wallet'},
        follow_redirects=True,
    )
    assert 'ルール「SAMPLE WALLET」を登録しました' in response.get_data(as_text=True)
    assert 'SAMPLE WALLET' in client.get('/rules').get_data(as_text=True)

    response = client.post(
        f'/transactions/{tx_id}/allocations',
        data={'alloc_category': ['食費', '外食'], 'alloc_amount': ['6000', '3000'], 'alloc_memo': ['', '']},
        follow_redirects=True,
    )
    assert '一致しません' in response.get_data(as_text=True)

    response = client.post(
        f'/transactions/{tx_id}/allocations',
        data={'alloc_category': ['食費', '外食'], 'alloc_amount': ['6000', '4000'], 'alloc_memo': ['a', 'b']},
        follow_redirects=True,
    )
    assert '内訳を保存しました' in response.get_data(as_text=True)
    assert '45,590' in client.get('/?month=2026-08').get_data(as_text=True)  # 総支出は変わらない

    html = upload(client, fixture_csv_bytes)
    assert 'このCSVはすでに取り込み済みです' in html and '件を取り込む' not in html
    token = html.split('name="token" value="')[1].split('"')[0]  # キャンセルフォームのトークンで確定を試みる
    body = client.post('/import/commit', data={'token': token}, follow_redirects=True).get_data(as_text=True)
    assert '新規の明細がありません' in body
    assert '要確認' in client.get('/review').get_data(as_text=True)

    # 取り消し: 確認文言に手動修正・内訳の件数が出て、明細と内訳が消え、ルールは残り、同じ CSV を再取込できる
    history = client.get('/import').get_data(as_text=True)
    assert (
        '(10 件)を取り消しますか?\n手動で変更したカテゴリ・メモ 1 件が失われます。\n内訳 2 件も削除されます。'
        in history
    )
    import_id = history.split('/undo"')[0].rsplit('/import/', 1)[1]
    body = client.post(f'/import/{import_id}/undo', follow_redirects=True).get_data(as_text=True)
    assert '明細 10 件を削除しました。 内訳 2 件も削除しました。' in body
    assert '取り消す' not in client.get('/import').get_data(as_text=True)
    assert client.get('/transactions').get_data(as_text=True).count('編集') == 0
    assert 'SAMPLE WALLET' in client.get('/rules').get_data(as_text=True)
    assert '10 件を取り込む' in upload(client, fixture_csv_bytes)
    body = client.post(f'/import/{import_id}/undo', follow_redirects=True).get_data(as_text=True)
    assert '取込履歴が見つかりません' in body


def test_invalid_month_and_year_params_are_handled(client: FlaskClient) -> None:
    """不正な month / year は無視し、解釈できる全角年は受け入れる

    Args:
        client: テストクライアント
    """
    assert client.get('/?month=abcdefg').status_code == 200
    assert client.get('/transactions?month=2026-13').status_code == 200
    assert client.get('/annual?year=20xx').status_code == 200
    assert client.get('/annual?year=²²²²').status_code == 200  # 上付き数字は \d（Nd）に含まれず int() もできない
    fallback = client.get('/annual?year=0000').get_data(as_text=True)  # 0000 は無効（前年リンクが負の年になる）
    assert f'{date.today().year}年の総支出' in fallback  # 明細が無いので今年に戻る
    assert '2026年の総支出' in client.get('/annual?year=２０２６').get_data(as_text=True)  # 全角数字は 2026 として読む


def test_rule_add_rejects_unknown_category(client: FlaskClient) -> None:
    """カテゴリ一覧に無いカテゴリのルールは登録されない

    Args:
        client: テストクライアント
    """
    response = client.post('/rules/add', data={'merchant_pattern': 'X', 'category': 'typo'}, follow_redirects=True)
    html = response.get_data(as_text=True)
    assert '不明なカテゴリです' in html and '→ typo' not in html


def test_external_back_is_replaced(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """back に外部 URL を渡しても戻り先はサイト内に限定される

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    import_csv(client, fixture_csv_bytes)
    tx_id = first_tx_id(client, 'SAMPLE')
    html = client.get(f'/transactions/{tx_id}/edit?back=https://evil.example').get_data(as_text=True)
    assert 'evil.example' not in html
    for back in ('//evil.example', '/\\evil.example/p', 'https://evil.example'):
        response = client.post(f'/transactions/{tx_id}/category', data={'category': 'その他', 'back': back})
        assert response.headers['Location'] == '/transactions'


def test_always_scope_applies_to_same_merchant(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """「今後この加盟店も」はルールに一致する他の明細にも反映され、件数が表示される

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    import_csv(client, fixture_csv_bytes)
    tx_id = first_tx_id(client, 'ホケン')
    response = client.post(
        f'/transactions/{tx_id}/category', data={'category': '保険・税金', 'scope': 'always'}, follow_redirects=True
    )
    assert '一致する 1 件にも適用しました' in response.get_data(as_text=True)
    response = client.post(
        f'/transactions/{tx_id}/allocations',
        data={'alloc_category': ['食費'], 'alloc_amount': ['abc'], 'alloc_memo': ['']},
        follow_redirects=True,
    )
    assert '内訳の金額が数値ではありません' in response.get_data(as_text=True)


def test_edit_changes_kind_only_when_posted(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """明細編集で収支を変えられ、kind を送らないフォーム（要確認画面）では収支を変えない

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    import_csv(client, fixture_csv_bytes)
    tx_id = first_tx_id(client, 'SAMPLE')
    assert 'name="kind"' in client.get(f'/transactions/{tx_id}/edit').get_data(as_text=True)
    response = client.post(
        f'/transactions/{tx_id}/category', data={'category': 'その他', 'kind': 'bogus'}, follow_redirects=True
    )
    assert '不明な収支です' in response.get_data(as_text=True)
    response = client.post(
        f'/transactions/{tx_id}/category', data={'category': 'その他', 'kind': 'income'}, follow_redirects=True
    )
    assert '収支を「収入」に変更しました' in response.get_data(as_text=True)
    response = client.post(f'/transactions/{tx_id}/category', data={'category': '外食'}, follow_redirects=True)
    assert '収支を' not in response.get_data(as_text=True)
    assert '+10,000' in client.get('/transactions?kind=income').get_data(as_text=True)


def test_clear_allocations_requires_existing_transaction(client: FlaskClient) -> None:
    """存在しない明細 ID の内訳削除は成功表示にせず404にする

    Args:
        client: テストクライアント
    """
    assert client.post('/transactions/not-found/allocations/clear').status_code == 404


def test_clear_allocations_removes_existing_rows(client: FlaskClient) -> None:
    """存在する明細の内訳はすべて削除できる

    Args:
        client: テストクライアント
    """

    def add_data(data: LedgerData) -> None:
        data.transactions.append(Transaction('tx_alloc', date(2026, 8, 1), 'A', 'A', 100, '食費'))
        data.allocations.append(Allocation('tx_alloc', '食費', 100))

    repo = client.application.extensions['smart_ledger'].repo
    repo.update(add_data)
    body = client.post('/transactions/tx_alloc/allocations/clear', follow_redirects=True).get_data(as_text=True)
    assert '内訳を削除しました' in body
    assert repo.load().allocations == []


def test_cross_site_post_is_rejected(client: FlaskClient) -> None:
    """Origin のホストが一致しない POST は 403、一致すれば受け付ける

    Args:
        client: テストクライアント
    """
    data = {'merchant_pattern': 'X', 'category': 'その他'}
    assert client.post('/rules/add', data=data, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/rules/add', data=data, headers={'Referer': 'https://evil.example/p'}).status_code == 403
    assert client.post('/rules/add', data=data, headers={'Origin': 'http://localhost'}).status_code == 302


def test_list_pages_render_with_confidence(client: FlaskClient) -> None:
    """confidence 付き（Jev 分類）の明細があってもダッシュボードと明細一覧が描画できる（macro から threshold を参照する）

    Args:
        client: テストクライアント
    """

    def add_tx(data: LedgerData) -> None:
        data.transactions.append(
            Transaction('tx_c', date(2026, 8, 1), 'A', 'A', 100, '食費', confidence=0.5, classification_source='jev')
        )

    client.application.extensions['smart_ledger'].repo.update(add_tx)
    for path in ('/?month=2026-08', '/transactions?month=2026-08'):
        html = client.get(path).get_data(as_text=True)
        assert 'class="review"' in html  # confidence 0.5 < 閾値 なので要確認の装飾が付く


def test_category_management_flow(client: FlaskClient) -> None:
    """カテゴリの追加 → 名称変更（ルールへ伝播） → 並び替え → 削除ガード → 削除

    Args:
        client: テストクライアント
    """
    body = client.post(
        '/categories/add', data={'name': '　ペット ', 'description': 'フード・動物病院'}, follow_redirects=True
    ).get_data(as_text=True)
    assert 'カテゴリ「ペット」を追加しました' in body  # 正規化後の名前で表示される
    assert 'フード・動物病院' in body

    body = client.post('/categories/add', data={'name': 'ペット'}, follow_redirects=True).get_data(as_text=True)
    assert '既に存在します' in body

    # ルールで使ってから名称変更すると伝播する
    client.post('/rules/add', data={'merchant_pattern': 'PETSHOP', 'category': 'ペット'}, follow_redirects=True)
    body = client.post(
        '/categories/edit',
        data={'category': 'ペット', 'new_name': 'ペット用品', 'description': ''},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert '名称変更を明細・ルール・内訳の 1 件に反映しました' in body
    assert 'ペット用品' in client.get('/rules').get_data(as_text=True)

    body = client.post(
        '/categories/move', data={'category': 'ペット用品', 'direction': 'up'}, follow_redirects=True
    ).get_data(as_text=True)
    assert '並び順を変更しました' in body

    body = client.post(
        '/categories/move', data={'category': '売電収入', 'direction': 'down'}, follow_redirects=True
    ).get_data(as_text=True)
    assert '既に端にあるため並び順は変わりません' in body

    body = client.post(
        '/categories/move', data={'category': 'その他', 'direction': 'sideways'}, follow_redirects=True
    ).get_data(as_text=True)
    assert '移動方向が不正です' in body

    body = client.post('/categories/delete', data={'category': 'ペット用品'}, follow_redirects=True).get_data(
        as_text=True
    )
    assert '使用中のため削除できません' in body

    body = client.post('/categories/delete', data={'category': 'その他'}, follow_redirects=True).get_data(as_text=True)
    assert '削除できません' in body

    client.post('/rules/delete', data={'merchant_pattern': 'PETSHOP'}, follow_redirects=True)
    body = client.post('/categories/delete', data={'category': 'ペット用品'}, follow_redirects=True).get_data(
        as_text=True
    )
    assert 'カテゴリ「ペット用品」を削除しました' in body
    assert 'ペット用品' not in client.get('/categories').get_data(as_text=True)


def test_rule_from_transaction_applies_across_billing_months(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """「今後この加盟店も」で登録したルールは、請求月だけ違う翌月の明細にも効く（旧仕様の同名ルールは置き換えられる）

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    client.post(
        '/rules/add', data={'merchant_pattern': 'テストデンリヨク 8ガツブン', 'category': '食費'}, follow_redirects=True
    )
    import_csv(client, fixture_csv_bytes)

    # fixture の「テストデンリヨク 8ガツブン」を編集。編集画面には請求月を除いたパターンが提案される
    tx_id = first_tx_id(client, 'テストデンリヨク')
    edit_html = client.get(f'/transactions/{tx_id}/edit').get_data(as_text=True)
    assert 'name="rule_pattern" id="rule-pattern" value="テストデンリヨク"' in edit_html

    body = client.post(
        f'/transactions/{tx_id}/category',
        data={'category': '住居・光熱', 'scope': 'always', 'memo': '', 'rule_pattern': 'テストデンリヨク'},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert 'ルール「テストデンリヨク」を登録しました' in body
    rules_html = client.get('/rules').get_data(as_text=True)
    assert 'テストデンリヨク 8ガツブン' not in rules_html and 'テストデンリヨク' in rules_html

    # 翌月分（9ガツブン）を含む別 CSV を取り込むと、Jev なしでもルールで分類される
    next_month = (
        fixture_csv_bytes
        + '2026/09/10,テストデンリヨク　９ガツブン,"7,900",,"7,900",１回払,,"7,900",,   ,\r\n'.encode('cp932')
    )
    body = import_csv(client, next_month, 'next.csv')
    assert 'ルール一致 1 件' in body
    listing = client.get('/transactions?q=9ガツブン').get_data(as_text=True)
    assert '<span class="badge cat">住居・光熱</span>' in listing and 'ルール' in listing


def test_rule_pattern_not_matching_transaction_warns(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """入力したパターンが当該明細に一致しない場合は登録はされるが警告が出る

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    import_csv(client, fixture_csv_bytes)
    tx_id = first_tx_id(client, 'サンプルカフェ')
    body = client.post(
        f'/transactions/{tx_id}/category',
        data={'category': '外食', 'scope': 'always', 'rule_pattern': 'ゼンゼンチガウミセ'},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert 'ルール「ゼンゼンチガウミセ」を登録しました' in body
    assert 'この明細の加盟店名に一致しません' in body


def test_always_scope_respects_more_specific_existing_rule(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """部分一致パターンで登録しても、より具体的な既存ルールが勝つ明細は上書きされない（件数表示も同じ判定）

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    client.post('/rules/add', data={'merchant_pattern': 'サンプルカフェ', 'category': '外食'}, follow_redirects=True)
    import_csv(client, fixture_csv_bytes)
    tx_id = first_tx_id(client, 'サンプルスーパー')
    assert 'data-url="/transactions/' in client.get(f'/transactions/{tx_id}/edit').get_data(as_text=True)
    assert client.get(f'/transactions/{tx_id}/rule-preview?pattern=サンプル').get_json() == {'count': 5}
    assert client.get(f'/transactions/{tx_id}/rule-preview?pattern=').get_json() == {'count': 0}
    assert client.get('/transactions/nope/rule-preview?pattern=x').status_code == 404

    body = client.post(
        f'/transactions/{tx_id}/category',
        data={'category': '食費', 'scope': 'always', 'rule_pattern': 'サンプル'},
        follow_redirects=True,
    ).get_data(as_text=True)
    # サンプルホケン ×2・サンプルツウシン・サンプル動画サービス利用料・サンプルショップ は反映され、
    # サンプルカフェ（既存の完全一致ルールが勝つ）は残る
    assert '一致する 5 件にも適用しました' in body
    cafe = client.get('/transactions?q=サンプルカフェ').get_data(as_text=True)
    assert '<span class="badge cat">外食</span>' in cafe and '<span class="badge cat">食費</span>' not in cafe


def test_import_undo_confirm_text_is_attribute_safe(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """ファイル名に引用符があっても確認文言は data-confirm 属性にエスケープされて描画され、取り消せる

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    # " はテストクライアントのマルチパート解析でファイル名が途中で切れるため、JS 文字列を壊す ' だけを使う
    import_csv(client, fixture_csv_bytes, "it's.csv")
    history = client.get('/import').get_data(as_text=True)
    assert 'data-confirm="取込「it&#39;s.csv」(10 件)を取り消しますか?' in history
    import_id = history.split('/undo"')[0].rsplit('/import/', 1)[1]
    body = client.post(f'/import/{import_id}/undo', follow_redirects=True).get_data(as_text=True)
    assert '明細 10 件を削除しました' in body


def test_annual_page_summary_and_links(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """年間表は月平均・年移動を表示し、通常カテゴリと未分類のセルから該当明細へ移動できる

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    import_csv(client, fixture_csv_bytes)

    def add_link_targets(data: LedgerData) -> None:
        data.transactions.append(Transaction('tx_none', date(2026, 7, 2), 'N', 'N', 300, ''))
        data.transactions.append(Transaction('tx_food', date(2026, 7, 3), 'F', 'F', 400, '食費'))

    client.application.extensions['smart_ledger'].repo.update(add_link_targets)
    html = client.get('/annual').get_data(as_text=True)
    assert '2026年の総支出' in html
    assert '45,590' in html  # 8 月の月間総支出（test_web_flow と同じ値）
    assert '16,013' in html and '明細のある 3 か月で割った値' in html
    food_path = '/transactions?month=2026-07&category=%E9%A3%9F%E8%B2%BB&kind=expense'
    unclassified_path = '/transactions?month=2026-07&category=%E6%9C%AA%E5%88%86%E9%A1%9E&kind=expense'
    assert f'href="{food_path.replace("&", "&amp;")}"' in html
    assert f'href="{unclassified_path.replace("&", "&amp;")}"' in html
    assert 'href="/annual?year=2025"' in html and 'href="/annual?year=2027"' in html
    assert '2027年の明細はありません' in client.get('/annual?year=2027').get_data(as_text=True)
    assert 'tx_food' in client.get(food_path).get_data(as_text=True)
    unclassified = client.get(unclassified_path).get_data(as_text=True)
    assert 'tx_none' in unclassified  # 未分類セルのリンク先に category が空の明細が出る
    assert '<option value="未分類" selected>' in unclassified  # 絞り込みフォームでも 未分類 が選ばれている


def test_annual_http_exports(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """年間表の CSV / Excel を正しい応答ヘッダーと内容でダウンロードできる

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    import_csv(client, fixture_csv_bytes)
    csv_response = client.get('/annual/export.csv?year=2026')
    assert csv_response.status_code == 200
    assert csv_response.headers['Content-Type'] == 'text/csv; charset=utf-8'
    assert csv_response.headers['Content-Disposition'] == 'attachment; filename="smart_ledger_2026.csv"'
    text = csv_response.get_data().decode('utf-8-sig')
    assert text.startswith('カテゴリ,1月,') and '月間総支出,' in text and ',45590,' in text

    xlsx_response = client.get('/annual/export.xlsx?year=2026')
    assert xlsx_response.status_code == 200
    assert xlsx_response.mimetype == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    assert xlsx_response.get_data()[:2] == b'PK'
    assert client.get('/annual/export.pdf').status_code == 404


def test_annual_http_exports_empty_year(client: FlaskClient) -> None:
    """明細が無い年も CSV / Excel をダウンロードできる

    Args:
        client: テストクライアント
    """
    csv_response = client.get('/annual/export.csv?year=2027')
    assert csv_response.status_code == 200
    lines = csv_response.get_data().decode('utf-8-sig').splitlines()
    assert lines[0].startswith('カテゴリ,1月,')
    assert lines[-1] == f'月間総支出,{",".join(["0"] * 13)}'

    xlsx_response = client.get('/annual/export.xlsx?year=2027')
    assert xlsx_response.status_code == 200
    assert xlsx_response.get_data()[:2] == b'PK'


def test_bank_csv_import_flow(client: FlaskClient, fixture_bank_csv_bytes: bytes) -> None:
    """銀行 CSV の取込 → ダッシュボードの収入・収支 → 年間表 → 収支での絞り込み

    Args:
        client: テストクライアント
        fixture_bank_csv_bytes: CP932 の銀行 fixture
    """
    html = upload(client, fixture_bank_csv_bytes, 'bank.csv')
    assert '5 件を取り込む' in html
    assert '<dt>口座</dt><dd>銀行口座</dd>' in html
    assert '4 件<span class="hint">(住宅ローン・売電収入以外の行)</span>' in html
    assert '地方税' not in html and '定額自動入金' not in html  # 対象外の行は一覧にも出さない

    body = import_csv(client, fixture_bank_csv_bytes, 'bank.csv')
    assert '5 件を取り込みました' in body
    assert 'うち 2 件は収入として記録しました' in body
    assert '要確認 0 件' in body  # 許可リストで確定するので要確認にならない

    # 2026-09: 支出 70,000 x 2 = 140,000 / 収入 7,000 / 収支 -133,000
    html = client.get('/?month=2026-09').get_data(as_text=True)
    assert '140,000' in html and '+7,000' in html and '-133,000' in html

    html = client.get('/annual?year=2026').get_data(as_text=True)
    assert '209,000' in html  # 年間の総支出 69,000 + 140,000
    assert '+13,500' in html  # 年間の収入 6,500 + 7,000
    assert '-195,500' in html  # 収支
    assert 'category=%E5%A3%B2%E9%9B%BB%E5%8F%8E%E5%85%A5&amp;kind=income' in html  # 収入行のリンクは収入だけに絞る

    html = client.get('/transactions?kind=income').get_data(as_text=True)
    assert '<span class="badge cat">売電収入</span>' in html  # カテゴリ選択の option とは別に明細行に出る
    assert '<span class="badge cat">住宅ローン</span>' not in html  # 支出は絞り込みで除かれる
    assert '収入 <strong class="income">+13,500 円</strong>' in html


def test_income_is_not_counted_as_spending_across_cards(
    client: FlaskClient, fixture_csv_bytes: bytes, fixture_bank_csv_bytes: bytes
) -> None:
    """カード明細と銀行明細を両方取り込んでも、総支出に収入が混ざらない

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 のカード fixture
        fixture_bank_csv_bytes: CP932 の銀行 fixture
    """
    import_csv(client, fixture_csv_bytes, 'card.csv')
    import_csv(client, fixture_bank_csv_bytes, 'bank.csv')

    # 8月 総支出 = カード 45,590 + 住宅ローン 69,000 = 114,590（売電 6,500 は含めない）
    html = client.get('/?month=2026-08').get_data(as_text=True)
    assert '114,590' in html and '+6,500' in html
    assert '-108,090' in html  # 収支


def test_dashboard_category_links_filter_by_kind(client: FlaskClient, fixture_bank_csv_bytes: bytes) -> None:
    """同じカテゴリが支出と収入の両方にある月も、ダッシュボードのカテゴリのリンク先はそれぞれの明細だけに絞る

    Args:
        client: テストクライアント
        fixture_bank_csv_bytes: CP932 の銀行 fixture
    """
    import_csv(client, fixture_bank_csv_bytes, 'bank.csv')
    tx_id = first_tx_id(client, '約定返済&month=2026-09')  # 2026-09 の住宅ローン 70,000 x 2 のうち 1 件を収入にする
    client.post(f'/transactions/{tx_id}/category', data={'category': '住宅ローン', 'kind': 'income'})

    html = client.get('/?month=2026-09').get_data(as_text=True)
    loan = '%E4%BD%8F%E5%AE%85%E3%83%AD%E3%83%BC%E3%83%B3'
    expense_path = f'/transactions?month=2026-09&category={loan}&kind=expense'
    income_path = f'/transactions?month=2026-09&category={loan}&kind=income'
    assert f'href="{expense_path.replace("&", "&amp;")}"' in html
    assert f'href="{income_path.replace("&", "&amp;")}"' in html
    assert '1 件 / 支出 <strong>70,000 円</strong></p>' in client.get(expense_path).get_data(as_text=True)
    assert '収入 <strong class="income">+70,000 円</strong>' in client.get(income_path).get_data(as_text=True)


def add_split_transactions(client: FlaskClient) -> None:
    """内訳のある明細（食費 1,000 円 → 日用品・買い物 600 + 外食 400）と内訳の無い外食 300 円を登録する

    Args:
        client: テストクライアント
    """

    def mutate(data: LedgerData) -> None:
        data.transactions.append(Transaction('tx_split', date(2026, 8, 5), 'KYASH', 'KYASH', 1000, '食費'))
        data.transactions.append(Transaction('tx_plain', date(2026, 8, 6), 'CAFE', 'CAFE', 300, '外食'))
        data.allocations.append(Allocation('tx_split', '日用品・買い物', 600, '洗剤'))
        data.allocations.append(Allocation('tx_split', '外食', 400, 'ランチ'))

    client.application.extensions['smart_ledger'].repo.update(mutate)


def test_allocation_rows_are_listed(client: FlaskClient) -> None:
    """明細一覧とダッシュボードの最近の明細に、内訳のカテゴリ・金額・メモの行が出る（スマホ用は折りたたみ）

    Args:
        client: テストクライアント
    """
    add_split_transactions(client)
    for path in ('/?month=2026-08', '/transactions?month=2026-08'):
        html = client.get(path).get_data(as_text=True)
        assert html.count('<tr class="tx-alloc') == 2  # PC: 明細行の下に内訳 2 行
        assert '<td class="alloc-memo">洗剤</td>' in html and '<td class="alloc-memo">ランチ</td>' in html
        assert '600 円' in html and '400 円' in html
        assert '<details class="tx-card-alloc" >' in html  # スマホ: 絞り込みなしでは閉じている
        assert '<summary>内訳 2 件</summary>' in html
        assert html.count('<details') == 1  # 内訳の無い明細には折りたたみを出さない


def test_category_filter_matches_allocation_categories(client: FlaskClient) -> None:
    """カテゴリの絞り込みは内訳のカテゴリにも一致させ、合計は明細金額で数える（内訳の金額を足し込まない）

    Args:
        client: テストクライアント
    """
    add_split_transactions(client)

    def summary(category: str) -> str:
        """カテゴリで絞り込んだ明細一覧の HTML を返す

        Args:
            category: 絞り込むカテゴリ
        """
        return client.get(f'/transactions?month=2026-08&category={category}').get_data(as_text=True)

    dining = summary('外食')  # 内訳の外食 400 と明細自身の外食 300 の両方が一致する
    assert 'tx_split' in dining and 'tx_plain' in dining
    assert '2 件 / 支出 <strong>1,300 円</strong>' in dining
    assert '内訳の金額は足し込みません' in dining
    assert '<tr class="tx-alloc last hit">' in dining and dining.count('hit"') == 2  # 一致した内訳行だけ強調する
    assert '<details class="tx-card-alloc" open>' in dining

    daily = summary('日用品・買い物')  # 内訳だけに出てくるカテゴリ
    assert 'tx_split' in daily and 'tx_plain' not in daily
    assert '1 件 / 支出 <strong>1,000 円</strong>' in daily

    food = summary('食費')  # 明細自身のカテゴリは従来どおり一致する
    assert 'tx_split' in food and '1 件 / 支出 <strong>1,000 円</strong>' in food
    assert '<details class="tx-card-alloc" >' in food  # 内訳に一致しなければ開かない
    assert '内訳の金額は足し込みません' in food  # 明細自身のカテゴリで一致しても内訳のある明細なら注記を出す

    plain = summary('外食&q=CAFE')  # 内訳のある明細が含まれなければ注記は出さない
    assert '1 件 / 支出 <strong>300 円</strong>' in plain
    assert '内訳の金額は足し込みません' not in plain
    assert 'tx_split' not in summary('交通')


def test_annual_hint_explains_list_totals_use_transaction_amount(client: FlaskClient) -> None:
    """年間表の注記は、明細一覧が内訳のカテゴリにも一致し合計は明細金額で数えることを説明する

    Args:
        client: テストクライアント
    """
    add_split_transactions(client)
    html = client.get('/annual?year=2026').get_data(as_text=True)
    assert '明細一覧は内訳のカテゴリにも一致しますが、件数・合計は明細金額で数えるため' in html
    assert '明細自身のカテゴリで絞り込む' not in html
