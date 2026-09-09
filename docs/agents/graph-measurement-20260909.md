# CEVDET Code Review Graph ölçümü — 9 Eylül 2026

Bu deney, başlangıç sembolleri önceden bilinen iki sorgu ve küçük bir PR'da
bağlam edinme maliyetini ölçer. Bu sorgu protokolünde graf metin/süre tasarrufu
sağlamadı; doğrudan ilişkileri doğru buldu ve PR için yapısal öncelik bilgisi verdi.
Sonuç doğal sorudan başlangıç bulmayı, dolaylı etki zincirlerini, mimari keşfi
ve tam bir model oturumunu ölçmez. Ortak bileşenlerdeki değişiklik ve çok
dosyalı incelemelerde graf kullanma kararı bu dar sonuçla elenemez.

## Yöntem ve sınır

- Sabit kod: `16d5e2139fb750045bb0a6717d284fd611f7bc96`; PR tabanı `37568e5`.
- Windows, Python 3.14.7, `code-review-graph==2.3.8`, ek embedding/Jedi kurulumu yok.
  Grafın 100 dosyasının tümü Git'te izlenen kaynaklarla eşleşti; metadata aynı HEAD'i gösterdi.
- Önceden seçilmiş üç görev, üç tekrar, dönüşümlü yöntem sırası; tabloda medyanlar var.
  Normal yöntem bütün depoyu okumaz: üç kod dizininin `git ls-files` çıktısı üzerinden
  `rg --no-ignore -n` ve PR için `git diff --unified=3` kullanır. Dosya listesi
  çıkarma süresi her normal arama örneğine dahildir; yalnız Git'te kayıtlı dosyalar aranır.
- Graf yolu `get_minimal_context` ile başlar. Gerekli tam liste için `minimal`
  sonucu genişletmenin maliyeti de sayılır. Dosya yolları kısaltılmadan, tek JSON
  gösteriminin karakterleri sayılır; MCP'nin metin/structuredContent kopyaları çift sayılmaz.
  Görev açıklamaları açıktır; ancak 2.3.8'de bu araç genel graf/diff özetini
  verir ve görev kelimelerini yalnız sonraki araç önerilerinde kullanır.
  Başlangıç sembolleri bu deneyde önceden verilir; doğal dil araması sınanmaz.
- İki yönteme aynı seçili uygulama/test kaynak okumaları eklenir. Bunlar kilit
  uygulaması ve testleri; snapshot yardımcıları/çağrı noktası/ilgili yarış testi;
  son PR'ın değişen üretim fonksiyonu ve iki test metodudur.
- Bu, Python API üzerinden **bağlam edinme akışının tekrarıdır**. Kör, iki ayrı
  model oturumunun karşılaştırması değildir. Süreye API/rg çalışması ve seçili
  kaynak okuması dahildir; bağımsız doğruluk karşılaştırması, model düşünmesi, MCP taşıması ve önceden yüklenmiş
  Python modüllerinin açılışı dahil değildir. Karakter sayısı gerçek model
  tokenı, cache faturası veya abonelik kotası değildir.
- Başarısız örnek tasarruf sayılmaz. Son koşudaki 18 örneğin tamamı başarılıdır.
  Ölçüm hazırlığında kaynak aralığı hatası veren iki koşu dışarıda bırakıldı.
  Kaynakla karşılaştırmada eksik/fazla çağıran varsa betik başarısız çıkar;
  zaman, metin ve yanlış ilişki kanıtları yine JSON'da korunur. Altı doğruluk
  kümesi (keşif çağıran/test; hata çağıran/çağrılan/test; PR sembolleri) her
  üç graf tekrarında karşılaştırılır: toplam 18 küme kontrolü. Kaynakla daha
  önce bağımsız doğrulanan test/PR kimlikleri sabit commit için betikte tutulur.
  Kasıtlı kısaltılmış ara yanıt yerine her sorgunun son, tamamlanmış yanıtı
  doğrulanır. Risk ve akış önceliklerinin anlamsal doğruluğu bu sayıya dahil değildir.
  Bu kontroller açık hata kontrolleridir; Python `-O` / `PYTHONOPTIMIZE` ile
  devre dışı kalmazlar.

## Sonuç

Toplam karakter, arama/graf yanıtı ile ortak kaynak doğrulamasının toplamıdır.

| Görev | rg toplam karakter | Graf toplam karakter | Graf farkı | rg süre | Graf süre |
|---|---:|---:|---:|---:|---:|
| `file_lock.locked`: çağıranlar ve testler | 20.614 | 71.802 | +%248,3 | 0,0340 sn | 0,1352 sn |
| `_stable_source_snapshot`: çağrı zinciri | 17.303 | 17.668 | +%2,1 | 0,0324 sn | 0,1498 sn |
| Son gerçek PR: değişen semboller | 31.095 | 32.076 | +%3,2 | 0,0537 sn | 0,2484 sn |

Ortak kaynak okuması sırasıyla 14.269, 14.895 ve 23.667 karakterdir.
Grafın yalnız sorgu çıktısı sırasıyla 57.533, 2.773 ve 8.409 karakterdir.
Tam yeniden oluşturma **18,22 sn**, değişikliksiz güncelleme **0,354 sn** sürdü;
bu bakım maliyetleri sorgu sürelerine dahil değildir. İlk sıfırdan kurulum ve
CI runner maliyeti ayrı ölçümlerdir.

## Kaynaktan bağımsız doğruluk kontrolü

Beklenen ilişkiler graf kullanılmadan `rg`, AST ve kaynak okumalarıyla çıkarıldı.

| Kontrol | Kaynakta beklenen | Grafın doğru bulduğu | Eksik / fazladan |
|---|---:|---:|---:|
| `locked` doğrudan üretim çağıranı | 44 | 44 | 0 / 0 |
| `locked` doğrudan test metodu | 9 | 9 | 0 / 0 |
| `_stable_source_snapshot` üretim çağıranı | 1 | 1 | 0 / 0 |
| Snapshot yardımcısının proje içi çağırdığı fonksiyon | 2 | 2 | 0 / 0 |
| PR'da değişen/eklenen fonksiyon veya test metodu | 3 | 3 | 0 / 0 |

Kilit testlerinin sekizi `test_file_lock.py::LockedContextTests`, biri
`test_codex_brain.py::VaultRetrievalTests.test_cache_is_read_under_lock_contention_but_not_rewritten`.
Snapshot yardımcısının çağıranı `build_vault_map`, çağırdıkları `_source_snapshot`
ve `_source_signature`. Hedefi adıyla çağıran doğrudan test yoktur; yarış testleri
entegrasyon kanıtıdır, yardımcının her retry dalını sınadıkları iddia edilmez.

PR'daki üç fonksiyon: `hook.handle_user_prompt`,
`PromptMemorySnapshotTests.test_profile_and_warning_share_the_checked_preference_snapshot`
ve `RetrievalRaceTests.test_hook_does_not_offer_raw_search_after_incomplete_retrieval`.
Graf ayrıca iki kapsayıcı test sınıfını değişen sembol olarak raporladı; bu bir
fonksiyon farkı değildir. Bu iki test sınıfını “test boşluğu” diye işaretlemesi
**yeni test gereksiniminin kanıtı değildir**. Risk skoru 0,55 ve iki etkilenen
akış yapısal önerilerdir; bağımsız hata veya iş riski doğrulaması yapılmadı.

Bu küme dinamik/alias/mock çağrıları, tam test kapsaması, silinen semboller veya
deponun bütün olası ilişkileri için doğruluk oranı vermez. Örneğin
`test_concurrency_queue.py` içindeki `real_lock` ve `test_second_brain_acceptance.py`
içindeki `real_handle` alias yolları bu doğrudan çağrı kümesinin dışındadır.

## Kurala dönüşen bulgular

Tek konumu bulma işi `rg` ile başlayabilir. Ortak bileşende davranış değişikliği,
çağıranların çağıranlarını izleme, mimari keşif ve PR etki incelemesi graf için
ayrı ve güçlü kullanım gerekçeleridir. [FAQ](https://github.com/tirth8205/code-review-graph/blob/main/docs/FAQ.md)
de grafın asıl değerini bu çok adımlı işlerde konumlandırır. Bu deney, grafın
bütün bu işlerdeki faydasını veya 35 dilli genel doğruluğunu kanıtlamaz.

Tekrarlanan geniş değerlendirme gerekirse yeni bir ölçüm motoru büyütmek yerine
hazır `agent_baseline` ve `multi_hop_retrieval` akışları CEVDET sorularıyla
değerlendirilir. İlki en iyi üç dosyayı okuyan bir simülasyondur; ikincisi
doğal sorgu → başlangıç sembolü → komşu ilişkileri zincirini sınar. İkisi de
tek başına gerçek model/kota ölçümü değildir. Bu tarihli betik dar deneyin
yeniden üretim kanıtı olarak kalır; ürün veya haftalık CI akışına eklenmez.

- `minimal`, `max_results=100` verilse bile kilidin 53 çağıranından 5'ini,
  9 testinden 5'ini gösterdi. Kalanlar tamamlanmadan inceleme bitirilemez.
  Tam JSON'ı otomatik büyütmek yerine hedefli kaynak araması tercih edilebilir.
- Otomatik inceleme POSIX/ripgrep 15.1.0 ortamında çoklu kök aramasında eksik
  sonuç bildirdi. Windows/ripgrep 15.2.0 kontrolünde iki sorgu 30'ar tekrarda
  aynı 6.345 / 2.408 karakteri verdi. Yeniden üretim için arama yine de açık
  Git dosya listesine sabitlendi; metin miktarları değişmedi, tablodaki süreler
  dosya listesini de çıkaran üç tekrarlı ölçüme aittir.
- Kısa `locked` adı 40 adayla belirsizdi. Ölçüm, iki yöntemde de bilinen dosya
  ve sembolü kullandı; graf sorgusunda tam `qualified_name` verildi.
- PR'ın `minimal` sonucu yalnız üç öncelik adı verdi; bütün değişen sembol
  listesi için genişletildi. “%99 tahmini tasarruf” göstergesi, iki değişen
  dosyanın tamamını okumaya kıyastı; yukarıdaki gerçek arama yöntemine kıyas değildi.
- Bu küçük örneklem, araç kataloğunu daraltmayı, Jedi/embedding kurmayı veya
  ayrı wiki/hafıza sistemi eklemeyi gerekçelendirmiyor.

## Yeniden üretme

İzole bir checkout'u yukarıdaki SHA'da tut. Paket 2.3.8 ve `rg` bulunan Python'la:

```powershell
python -X utf8 -m code_review_graph build --repo <sabit-checkout>
python -X utf8 <rapor-checkout>/docs/agents/measure-graph-20260909.py <sabit-checkout> <yerel-sonuc.json>
python -X utf8 -m code_review_graph update --repo <sabit-checkout>
```

Script ham sorgu payload'larını, üç tekrarın ölçülerini ve kaynak AST manifestini
yerel JSON'a yazar. Ürün kodunu import etmez, model çağırmaz, grafı değiştirmez.
Kaynak aralıkları/commit sabittir; başka sürümde aynı deneyi yaptığını iddia etmez.
Ölçüm scripti teknik kanıttır, ürünün çalışma akışına eklenmez.

Kaynak dayanağı: [README](https://github.com/tirth8205/code-review-graph/blob/2c6dae32643572ee528eb9b77dbcc17f58f3a8c9/README.md),
[User Guide](https://github.com/tirth8205/code-review-graph/blob/2c6dae32643572ee528eb9b77dbcc17f58f3a8c9/docs/USAGE.md),
[benchmark yöntemi](https://github.com/tirth8205/code-review-graph/blob/2c6dae32643572ee528eb9b77dbcc17f58f3a8c9/docs/REPRODUCING.md),
[GitHub Action](https://github.com/tirth8205/code-review-graph/blob/2c6dae32643572ee528eb9b77dbcc17f58f3a8c9/docs/GITHUB_ACTION.md).
Yukarıdaki CEVDET ölçüleri bu kaynakların iddiası değil, yerel deney sonucudur.
