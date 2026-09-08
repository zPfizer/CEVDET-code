from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


VAULT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(VAULT / ".codex" / "scripts"))

import vault_retrieval  # noqa: E402


EXPECTED_QUERY_SOURCES = {
    "ALARK": "tweet-alark-125-42.md",
    "GUBRF": "gorsel-paket-58-gubrf-fincan-kulp-matematik-foyu.md",
    "tweezer bottom talep bolgesi": "tweet-tweezer-bottom-talep-bolgesi-backtest.md",
    "KAP fiili dolasim": "tweet-fiili-dolasim-yuzde-30-alti-stratejik-taramalar.md",
    "4 saat order block": "tweet-fibo-0618-bolge-h4-order-block-multitimeframe.md",
    "THYAO order book": "gorsel-paket-126-bist-order-book-derinlik-bmp-fit.md",
    "VIX BIST100": "gorsel-paket-114-bist-vix-python-master-analiz.md",
    "yahoo chart backtest sonucu": "tweet-williams-r-sma-swing-strateji-backtest.md",
}


def _synthetic_corpus(root: Path) -> vault_retrieval.VaultMap:
    tansu = root / "🏰 300-Projects" / "Tansu X Veri Havuzu"
    tansu.mkdir(parents=True)
    for query, file_name in EXPECTED_QUERY_SOURCES.items():
        symbol = query if query.isupper() and " " not in query else ""
        path = tansu / file_name
        path.write_text(
            "---\n"
            f"title: {file_name}\n"
            "tags: [tansu/teknik/formasyon]\n"
            f"symbols: [{symbol}]\n"
            "---\n"
            f"# {file_name}\n\n{query}\n",
            encoding="utf-8",
        )
    a1cap = tansu / "gorsel-paket-56-a1cap-weinstein-dort-evre.md"
    a1cap.write_text(
        "---\n"
        "title: A1CAP teknik vaka\n"
        "tags: [tansu/teknik/formasyon]\n"
        "symbols: [A1CAP]\n"
        "---\n# A1CAP teknik vaka\n",
        encoding="utf-8",
    )
    return vault_retrieval.build_vault_map(root, write_cache=False)


class SemanticRetrievalPropertyTests(unittest.TestCase):
    def test_exact_symbol_property_is_a_single_term_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            tansu = vault / "🏰 300-Projects" / "Tansu X Veri Havuzu"
            tansu.mkdir(parents=True)
            (tansu / "case.md").write_text(
                "---\n"
                "title: Teknik Vaka\n"
                "tags: [tansu/teknik/formasyon]\n"
                "symbols: [A1CAP]\n"
                "---\n"
                "# Teknik Vaka\n",
                encoding="utf-8",
            )

            entries = vault_retrieval.build_vault_map(vault, write_cache=False)
            hits = vault_retrieval.search_vault(entries, "A1CAP", route="TANSU")

        self.assertEqual([hit.entry.path for hit in hits], [
            "🏰 300-Projects/Tansu X Veri Havuzu/case.md"
        ])

    def test_semantic_properties_ignore_sentinel_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            tansu = vault / "🏰 300-Projects" / "Tansu X Veri Havuzu"
            tansu.mkdir(parents=True)
            (tansu / "case.md").write_text(
                "---\n"
                "title: Veri Vakası\n"
                "tags: [tansu/veri/veri-kaynağı]\n"
                "symbols: []\n"
                "timeframes: []\n"
                "evidence_types: [kaynak-görseli]\n"
                "data_sources: [not-provided]\n"
                "---\n"
                "# Veri Vakası\n",
                encoding="utf-8",
            )

            entries = vault_retrieval.build_vault_map(vault, write_cache=False)

        self.assertEqual(
            vault_retrieval.search_vault(entries, "not provided", route="TANSU"),
            [],
        )
        self.assertEqual(
            vault_retrieval.search_vault(entries, "kaynak görseli", route="TANSU")[0].entry.path,
            "🏰 300-Projects/Tansu X Veri Havuzu/case.md",
        )

    def test_synthetic_a1cap_query_returns_the_source_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entries = _synthetic_corpus(Path(temporary))
            hits = vault_retrieval.search_vault(entries, "A1CAP", route="TANSU")

        self.assertTrue(hits)
        self.assertEqual(
            hits[0].entry.path,
            "🏰 300-Projects/Tansu X Veri Havuzu/"
            "gorsel-paket-56-a1cap-weinstein-dort-evre.md",
        )

    def test_synthetic_tansu_queries_return_the_expected_primary_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entries = _synthetic_corpus(Path(temporary))
            for query, file_name in EXPECTED_QUERY_SOURCES.items():
                with self.subTest(query=query):
                    hits = vault_retrieval.search_vault(
                        entries,
                        query,
                        route="TANSU",
                    )
                    self.assertTrue(hits)
                    self.assertEqual(Path(hits[0].entry.path).name, file_name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
