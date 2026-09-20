"""Flask test client による画面フローのテスト"""

from __future__ import annotations

import io
from datetime import date

import pytest
from flask.testing import FlaskClient

from smart_ledger import create_app
from smart_ledger.config import Config
from smart_ledger.models import LedgerData, Transaction


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


def test_pages_render(client: FlaskClient) -> None:
    """データが無い状態でも各画面が 200 を返す

    Args:
        client: テストクライアント
    """
    for path in ('/', '/transactions', '/review', '/rules', '/categories', '/import', '/health'):
        assert client.get(path).status_code == 200


def test_web_flow(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """取込 → ダッシュボード → カテゴリ変更（ルール登録） → 内訳 → 再取込拒否 の一連のフロー

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

    tx_html = client.get('/transactions?q=SAMPLE').get_data(as_text=True)
    tx_id = tx_html.split('/transactions/')[1].split('/edit')[0]
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

    assert 'このCSVはすでに取り込み済みです' in upload(client, fixture_csv_bytes)
    assert '要確認' in client.get('/review').get_data(as_text=True)


def test_invalid_month_param_is_ignored(client: FlaskClient) -> None:
    """不正な month はエラーにならず無視される

    Args:
        client: テストクライアント
    """
    assert client.get('/?month=abcdefg').status_code == 200
    assert client.get('/transactions?month=2026-13').status_code == 200


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
    token = upload(client, fixture_csv_bytes).split('name="token" value="')[1].split('"')[0]
    client.post('/import/commit', data={'token': token})
    tx_html = client.get('/transactions?q=SAMPLE').get_data(as_text=True)
    tx_id = tx_html.split('/transactions/')[1].split('/edit')[0]
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
    token = upload(client, fixture_csv_bytes).split('name="token" value="')[1].split('"')[0]
    client.post('/import/commit', data={'token': token})
    tx_html = client.get('/transactions?q=ホケン').get_data(as_text=True)
    tx_id = tx_html.split('/transactions/')[1].split('/edit')[0]
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
        '/categories/move', data={'category': 'その他', 'direction': 'down'}, follow_redirects=True
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
    """「今後この加盟店も」で登録したルールは、請求月だけ違う翌月の明細にも効く

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    token = upload(client, fixture_csv_bytes).split('name="token" value="')[1].split('"')[0]
    client.post('/import/commit', data={'token': token}, follow_redirects=True)

    # fixture の「テストデンリヨク 8ガツブン」を編集。編集画面には請求月を除いたパターンが提案される
    tx_html = client.get('/transactions?q=テストデンリヨク').get_data(as_text=True)
    tx_id = tx_html.split('/transactions/')[1].split('/edit')[0]
    edit_html = client.get(f'/transactions/{tx_id}/edit').get_data(as_text=True)
    assert 'name="rule_pattern" value="テストデンリヨク"' in edit_html

    body = client.post(
        f'/transactions/{tx_id}/category',
        data={'category': '住居・光熱', 'scope': 'always', 'memo': '', 'rule_pattern': 'テストデンリヨク'},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert 'ルール「テストデンリヨク」を登録しました' in body

    # 翌月分（9ガツブン）を含む別 CSV を取り込むと、Jev なしでもルールで分類される
    next_month = (
        fixture_csv_bytes
        + '2026/09/10,テストデンリヨク　９ガツブン,"7,900",,"7,900",１回払,,"7,900",,   ,\r\n'.encode('cp932')
    )
    token = upload(client, next_month, 'next.csv').split('name="token" value="')[1].split('"')[0]
    body = client.post('/import/commit', data={'token': token}, follow_redirects=True).get_data(as_text=True)
    assert 'ルール一致 1 件' in body
    listing = client.get('/transactions?q=9ガツブン').get_data(as_text=True)
    assert '住居・光熱' in listing and 'ルール' in listing


def test_rule_pattern_not_matching_transaction_warns(client: FlaskClient, fixture_csv_bytes: bytes) -> None:
    """入力したパターンが当該明細に一致しない場合は登録はされるが警告が出る

    Args:
        client: テストクライアント
        fixture_csv_bytes: CP932 の fixture
    """
    token = upload(client, fixture_csv_bytes).split('name="token" value="')[1].split('"')[0]
    client.post('/import/commit', data={'token': token}, follow_redirects=True)
    tx_html = client.get('/transactions?q=サンプルカフェ').get_data(as_text=True)
    tx_id = tx_html.split('/transactions/')[1].split('/edit')[0]
    body = client.post(
        f'/transactions/{tx_id}/category',
        data={'category': '外食', 'scope': 'always', 'rule_pattern': 'ゼンゼンチガウミセ'},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert 'ルール「ゼンゼンチガウミセ」を登録しました' in body
    assert 'この明細の加盟店名に一致しません' in body
