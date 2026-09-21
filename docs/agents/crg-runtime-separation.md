# CRG runtime sahipliği

Bu dosya taşıma sözleşmesini tanımlar; kurulum veya canlı kabul sonucu değildir.
Hook, görev ve MCP çağrıları canonical Python executable'ının mutlak yolunu
kullanır. Kullanıcı veya sistem PATH'i okunmaz, değiştirilmez ve rollback'e
alınmaz. Registry transaction/TxF bu taşımanın parçası değildir.

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
rollback eski hook/config/task action ve kurulum kimliğini geri getirir. Her yüzey
değişmeden önce beklenen içerikle karşılaştırılır; gözlenen yabancı değişiklikte
durur. Dosya değişiminde gerçek önceki içerik native ReplaceFileW yedeğinde
korunur. İki görev önce devre dışı bırakılır ve eski watcher süreçlerinin
sonlandığı doğrulanır. Görevde yalnız sahip olunan action alanları güncellenir;
diğer alanlar güncel snapshot'tan korunur. Geri okunan tam tanım hedefle
karşılaştırılır; beklenmeyen farkta en fazla iki retry yapılır. Bu protokol
OS seviyesinde compare-and-swap veya CRASH_ATOMICITY garantisi değildir.
Eski görevlerin action/path değerleri geri getirilir; rollback'te iki görev de
devre dışı kalır. Böylece rollback sırasında bir tetikleyici Vault
taramasını veya eski venv'de bytecode yazımını başlatamaz. Bu çalışma-durumu farkı
kanıtta açıkça belirtilir; birebir process-state rollback diye sunulmaz.

Canlı kabul: gerçek code commit'inde hook isteği, native scheduled watcher'ın
yenilemesi, yeni HEAD/hash/çağrı ilişkisi ve yeni ortamdan MCP JSON-RPC yanıtı
birlikte doğrulanır. Sentetik testler tek başına canlı kabul değildir. Kurulum
raporu ayrıca sekiz istenen bağın dışındaki Vault hook/config bağlarını belirtir.
PATH'te kalan tarihsel metin, mutlak executable kullanan bu runtime çağrılarının
bağımlılığı değildir; PATH temizliği bu işin kapsamı dışındadır.
