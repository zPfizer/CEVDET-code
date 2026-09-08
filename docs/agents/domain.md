# Domain docs

Bu belge kod deposunda çalışan ajanlar için domain dokümanlarının kullanımını tanımlar.

## Okuma sırası

1. Kök `CONTEXT.md` dosyasını oku.
2. Değişen alanla ilgili `docs/specs/` sözleşmesini oku.
3. Varsa ilgili ADR ve kaynak kodu oku.
4. Etkilenen testleri ve mevcut çağrıları incele.

Eksik bir doküman için bu depoya kişisel Vault kaynağı kopyalama. Gerekli teknik karar için küçük bir belge değişikliği yap veya kararı PR'da açıkça belirt.

## Dil ve otorite

Çıktılarda `CONTEXT.md` içindeki terimleri kullan: populated Vault, runtime code, synthetic fixture, provenance ve fail-closed. Kaynak kod ve test davranışın canlı otoritesidir. Doküman ile kod çelişirse çelişkiyi raporla ve sözleşmeyi bilinçli olarak güncelle.

## Sınırlar

- Bu depo dolu Vault değildir; kişisel notlar, günlükler, bilgi görünümleri ve özel ayarlar burada tutulmaz.
- `.codex/config.toml` hook'ları kapalıdır. `.codex/hooks.json` yalnız taşınabilir dağıtım tanımıdır.
- Testler synthetic fixture kullanır. Başarılı testleri canlı Vault/App/model kanıtı olarak sunma.
- Açık hedef Vault ve gerçek yetki olmadan model ingestion, dış yazma, yayınlama veya canlı aktivasyon başlatma.
- Hedef Vault uygulamasında yalnız onaylanmış runtime kodunu kullan; notları, config'i, `AGENTS.md`'yi veya çalışan dosyaları kopyalama.

Kalıcı veri, gizlilik, concurrency, recovery veya fail-closed davranışına dokunan değişikliklerde sözleşmedeki invariants ve ilgili test kapılarını birlikte denetle.
