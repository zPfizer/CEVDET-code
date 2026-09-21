# CRG runtime sahipliği

Bu dosya henüz kurulmamış taşıma adayını tanımlar. Yerel Windows ortamında
işlemli kayıt defteri açılışı `ERROR_RM_NOT_ACTIVE (6801)` verdiği için PATH
değişikliği doğrulanamadı. Task Scheduler güncellemesinde setter aralığındaki
harici değişikliği koruma açığı da kapanmadı. Bu aday kurulum onayı değildir.

CRG 2.3.8 mevcut kullanıcı seviyesindeki `~/.codex/cevdet-codex/integrations/crg/.venv`
ortamından çalışır. Ortam Vault ve code worktree yaşam döngülerinden ayrıdır;
mevcut canonical kurulumla aynı sahiplik ve bağımlılık kaydını kullanır. Yeni
venv, servis veya daemon eklenmez. Eski Vault venv geri dönüş için korunur.

Her code indeksinin kalıcı yazıcısı mevcut Windows görevi içindeki native CRG
watcher'dır. Beş Git hook'u canonical code kökündeki, kök başına ayrı atomik
yenileme isteğini günceller. Native watcher'ın
mevcut `stop_event` arayüzü isteği görünce izleme turunu kapatır, aynı sahiplik
altında full build yapar ve izlemeye devam eder. Böylece yalnız commit/branch
kimliği değiştiğinde upstream'in metadata güncellememe boşluğu korunmaz.
Görev başına başka bir daemon veya ikinci zamanlayıcı yoktur.

Watcher yaşam süresince mevcut freshness kilidini tutar. Başka watcher veya
global guarded refresh aynı indekste yazıcı olamaz. Sahibi belirsiz kilit
çalınmaz; kurucu yalnız durdurulduğu kanıtlanan kendi watcher PID'sinin marker'ını
temizler. Worktree indeksleri ayrı kalır; mevcut Code görevi doğrulanmış worktree
isteklerini sırayla kendi sürecinde ve ilgili indeks kilidi altında işler.
Yenileme sırasında gelen yeni istek nesli sonraki turda korunur. Worktree başına
yeni scheduled task veya sürekli çalışan watcher oluşturulmaz.

Proje MCP'si iki sorgu aracını sunar. Kaynak/root/commit/hash kimliği eksik veya
eskiyse kaynak araması gerekir; sorgu indeksi yeniden oluşturmaz. Upstream
GraphStore açılışı şema yazabildiğinden, aynı sorgu metotları salt okunur SQLite
bağlantısıyla çalıştırılır. Paket dosyalarına yama yapılmaz. Bu uyarlama yalnız
sabitlenmiş 2.3.8 ve schema 9 ile doğrulanır.
Sorgu ayrıca yalnız postprocessing sonrası yayımlanan kaynak-digest kaydını
kabul eder. Aynı kaynak yeniden oluşturulurken kayıt önce geçersizleştirilir;
sorgu sırasında yayımlanan nesil değişirse sonuç güncel diye sunulmaz.

CEVDET-code ve onun doğrulanmış Git worktree'leri kod kapsamıdır. Vault, izin
verilmiş source root değildir. Vault scheduled task'ı yalnız korumalı durum
bildirir; CRG watch/build çağırmaz. Vault hook'ları içerik okumadan durum bildirir.
Secret, oversize, NO_EXTERNAL, DENY, trust, hafıza aktivasyonu ve 8000B context
sınırları bu fiziksel taşıma ile değiştirilmez.

`crg_install.py` sabit başlangıç yedeğini prepare ile üretir, source hash'leriyle
eşleşen bağımsız review kaydından sonra apply yapar. Aynı state üzerinden
rollback eski hook/config/task/PATH ve kurulum kimliğini geri getirir. Her yüzey
değişmeden önce beklenen içerikle karşılaştırılır; gözlenen yabancı değişiklikte
durur. Dosya değişiminde gerçek önceki içerik native ReplaceFileW yedeğinde
korunur. PATH aynı registry transaction içinde okunup karşılaştırılır ve yazılır;
transaction desteği yoksa yazım durur. Task Scheduler için gözlemler arasındaki
harici yazımı dışlayan koruma henüz sağlanmamıştır.
Eski görevlerin action/path değerleri geri getirilir; iki görev de XML içinde
`Enabled=false` ile kaydedilir. Böylece rollback sırasında bir tetikleyici Vault
taramasını veya eski venv'de bytecode yazımını başlatamaz. Bu çalışma-durumu farkı
kanıtta açıkça belirtilir; birebir process-state rollback diye sunulmaz.

Canlı kabul: gerçek code commit'inde hook isteği, native scheduled watcher'ın
yenilemesi, yeni HEAD/hash/çağrı ilişkisi ve yeni ortamdan MCP JSON-RPC yanıtı
birlikte doğrulanır. Sentetik testler tek başına canlı kabul değildir. Kurulum
raporu ayrıca sekiz istenen bağın dışındaki Vault hook/config ve PATH bağlarını
saymalıdır. Bu belge kurulum veya canlı kabul sonucu değildir.
