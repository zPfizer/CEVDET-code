# Canlı hafıza teslim ve kabul planı

Durum: hazırlık planı; canlı aktarım ve etkinleştirme kabulü değildir.

Bu belge beş hazırlık işini birlikte sınar. PR, merge ve hedef Vault'a teslim
yetkisinin sahibi [PR çalışma düzenidir](pr-workflow.md); burada yeni bir onay
veya izin mekanizması tanımlanmaz. Ürün davranışının sahibi
[ikinci beyin sözleşmesidir](../specs/ikinci-beyin-sozlesmesi.md).

## Beş hazırlık işi

| İş | Kodda hazırlanacak sonuç | Ayrı canlı kanıt |
| --- | --- | --- |
| Windows süreç sonlandırma | Mevcut süreç/worker cleanup ve kalıcı hata sınırlarının uyumlu hedef sürümü | Gerçek Windows host altında sonlanma; belirsiz süreçte yeniden iş başlatmama |
| Kod ve hedef Vault eşleştirmesi | Hedefin hash tabanı, seçili commitler, bağımlılıklar ve korunacak hedef farkları | Onaylı aktarım sonrası ilgili testler ve hedef doctor |
| Oturumlar arası hafıza | Mevcut kayıt, suppression ve kaynaklı okuma yolunun kabul senaryoları | Kullanıcının yeni App oturumunda verdiği bilgi ve başka oturumda kaynaklı hatırlama |
| Arama regresyonu | Sentetik alias, uzun not, görsel açıklaması, arşiv ve proje soruları | Hedef korpusta gerçek arama; sentetik sonuç bunun yerine geçmez |
| Hook davranışı | Niyet/gizlilik, sınırlı teslim, hata bildirimi ve salt okunur arama düzeltmeleri | Gerçek host olaylarının teslimi ve aşağıdaki sonuçlar |

## Hedef sürümün hazırlanması

1. Yeni hedefi açıkça belirle; hook etkinliğini dosyadaki tanımlardan ayrı doğrula.
   App kapalı gösteriyorsa `enabled=true` kaydı etkinlik kanıtı sayılmaz.
2. Kaynak main, seçilen PR head'leri ve hedef runtime hashlerini kaydet.
   Kaynak main'de zaten çözülmüş soruna aynı değişikliği içeren yeni PR açma.
3. [Concurrency entegrasyonundaki](concurrency-integration-20260909.md) korumaları
   tek dosya kopyasıyla parçalama. Süreç temizliği; `process_control.py`,
   `worker_supervisor.py`, `codex_runner.py` ve flush/compile hata aktarımını
   birlikte gerektirir. Yayın/okuyucu korumaları `compile_state.py`,
   `state_store.py`, `memory_ledger.py`, `companion_memory.py` ve retrieval ile
   ilişkilidir. Güncel gerçek çağrılar nihai dosya listesini belirler.
4. Daha önce hedefe uygulanmış kaynak kapsamı, gizlilik, geçmiş sıralaması ve
   Base doğrulama düzeltmelerini koru. Dosyanın tamamını eski main kopyasıyla
   değiştirmek güncel hedef düzeltmesini geri alabilir.
5. Ayrı aday worktree'de anlam farkını ve birleştirilmiş testleri incele.
   Aynı dosyaya dokunan bağımsız PR'lar birleştirilince temiz Git merge sonucu
   tek başına davranış uyumluluğu sayılmaz.
6. Not, daily/knowledge, özel ayar, AGENTS ve runtime kurtarma girdilerini
   otomatik kopyalama. Hedef test uyarlamasını kod değişikliğinden ayrı göster.
   Kaynaklar ve geri dönüş için gereken durum korunmadan aktarım tamamlandı deme.

Kod hazırlığı, gerçek model çağrısı veya App etkinleştirmesi için yetki değildir.
Yerel test, Windows CI, otomatik inceleme, merge, hedef aktarım ve canlı kabul
ayrı sonuçlardır; her biri yalnız gerçekten tamamlandığında kaydedilir.

### 9 Eylül 2026 kaynak eşleme notu

Karşılaştırılan kod tabanı `16d5e21` süreç/concurrency çözümlerini zaten içerir:
`033d1e6`, `99cac84`, `0ba191e`, `be92592` ve `2d91d46`. Bunlar için tekrar
aynı düzeltmeyi geliştirmek yerine hedefteki karşılıkları doğrulanır.

O tarihte açık PR'ların ortak dosya sırası: `#6 → #9`, `#8 → #12`,
`#11 → #16`, `#7 → #18`, `#9 → #14`, `#17 → #19`; `#19`, `#9/#14/#17`
sonrasında ortak doctor/corpus farkıyla yeniden değerlendirilir. `#10`,
`#13` ve belge PR'ı `#15` bu gruplardan bağımsızdır. Ok yalnız ortak dosya
entegrasyon sırasını önerir; otomatik merge yetkisi değildir. Yeni hook PR'ları
da aynı dosyalara dokunduğundan bu tarihli liste nihai uyumluluk kanıtı sayılmaz.

Karşılaştırılan hedefte `#9` ve `#16–19` davranışları zaten bulunuyordu;
yalnız kaynak main'i kopyalamak bunları geri alabilirdi. Belge PR'ı `#15`
runtime aktarım listesine girmez. Her sonraki teslimde bu durum yeniden okunur;
bu not herhangi bir hedefin güncel uygulama durumunun kalıcı otoritesi değildir.

### Hazırlık PR'ları ve ortak değişiklikler

| PR | Hazırlanan sonuç |
| --- | --- |
| #20 | Hook yetki metninin mevcut konuşma ve hedefle sınırlanması |
| #21 | Yirmi sentetik arama sorgusu ve sıralamaya giren rakip kaynaklar |
| #22 | Çözülememiş terminal kayıtların sonraki oturumda görünmesi |
| #23 | Doğal kaydetmeme ifadeleri ve alıntılı hedeflerin korunması |
| #24 | Bu teslim ve gerçek App kabul planı |
| #25 | Salt okunur incelemeden açık uygulama isteğine geçiş |
| #26 | Kaynak bağlantılarını koruyan yazmasız, filtrelenmiş okuma |
| #27 | Hook süre sınırı, kalıcı teslim ve kuyruk kurtarma |

Bu PR'ların son head, CI ve inceleme durumları teslim anında yeniden okunur.
Birleştirmede #22'nin `terminal` bilgisi ile #27'nin `orphan_hook_inputs`
bilgisi hem kuyruk sonucunda hem digest'te korunur; hook terminal uyarısı ve
kurtarma tetiklemesi birlikte kalır. #25'in koşullu enqueue testleri #27'nin
`deadline` argümanıyla birlikte doğrulanır. #23, #25 ve #26'nın ortak
`memory_ledger.py` değişiklikleri birbirini düşürmemelidir.
Örneğin #22 önce merge edilirse #27 güncel main üzerine yeniden tabanlanır;
ortak kuyruk, digest ve hook testleri bu birleşik sürümde tekrar çalıştırılır.

Yalnız bu sekiz PR'ı birleştiren test, önceki #6–19 paketlerinin birlikte
çalıştığını veya hedef Vault'un güncellendiğini kanıtlamaz.

## Gerçek App kabulü

Etkinleştirme yetkisinden sonra, olayın zamanı ve oturum kimliği ile runtime
kaydını eşleştir. Terminalden elle üretilen receipt App teslimi sayılmaz.
Zamanı eski, başka köke veya başka oturuma ait kanıtı güncel kabul yerine koyma.

| Senaryo | Başarı ölçütü |
| --- | --- |
| Yeni zararsız bilgi | Kullanıcı yeni App oturumunda bilgiyi kendisi verir; Stop sonrası kaynaklı günlük/oturum kaydı oluşur. |
| Sonraki oturum | Yeni oturum önceki bilgiye konuşma geçmişinden değil, okunan kalıcı kaynaktan cevap verir; kaynak ve anlam eşleşir. |
| Tekrarlı olay | Aynı içerik için Stop, PreCompact ve SessionEnd tek kayıt üretir; coverage boş yere tekrar özetletmez. |
| Kaydetmeme | “Bunu kaydetme, lütfen” gibi ifade ilgili katkıyı ve onu tekrarlayan yanıtı model girdisi/kayıt dışında bırakır; ilgisiz katkı korunur. |
| Geç gelen oturuma özel tutma | Önce bir kayıt yayımlanır; sonra kullanıcı “bu konuşmada kalsın” der. Ajan mevcut `suppress_derived_memory` yoluyla geçmiş karşılıkları dışlar; kaynakları silmeden yeni oturum okuması ve derleme girdisinde dışlama doğrulanır. Yalnız session marker varlığı başarı değildir. |
| İncelemeden uygulamaya | Audit sonrası eylem isteyen soru doğru kapsamı açar. Olumsuzluk, alıntı ve aynı mesajdaki açık salt okunur sınır bunu açmaz. |
| Proje ve dış işlem | Açık istek ilgili depoda yürütülebilir; proje kodu Vault notlarına yazılmaz. Dış işlem yalnız açık kullanıcı yetkisiyle yapılır. |
| Salt okunur arama | Arama cache ve filtrelenmiş içerik dosyalarını değiştirmeden doğru kaynağı sunar; teknik sağlık metadata'sı içerik yazımıyla karıştırılmaz. |
| Kapanış ve sıkıştırma | Gerçek host olayları alınır; `SessionEnd` matcher'ı gerçek kapanma nedeniyle eşleşir. Kapanış olayı olmadan uygulama kesilmesi ayrıca sınır olarak gösterilir. |
| Kesinti ve kilit | Kuyruk kabulü gecikince kaynak/teslim girdisi korunur. Yarım çok dosyalı yayın sağlıklı görünmez; sahipliği belirsiz süreç tekrar başlatılmaz. |
| Başarısız kayıt | Gerçek unresolved terminal hata görünürdür; doğrulanmış successor ile kurtarılmış geçmiş kayıt yeni kayıp gibi bildirilmez. |
| Anlam ve kaynak | Öneri kullanıcı kararı yapılmaz; ara soru ana işi silmez; kaynaktaki belirsizlik doğrulanmış dış olguya dönüşmez. |

Geç gelen gizlilik tercihi mevcut tasarımda agent destekli kapsam çözümünü
gerektirir. Bu kabul başarılı olmadan bütün oturumun özel kaldığı söylenmez.
Mevcut yol yeterliyse yeni bir gizlilik altyapısı eklenmez; başarısız senaryonun
kanıtı hangi kod değişikliğinin gerekli olduğunu belirler.

## Kabul kaydı

Hedefe ait kayıt özel Vault'un mevcut teknik çalışma alanında tutulur; kişisel
kaynak veya eski Vault Git geçmişi bu kod deposuna taşınmaz. Kayıt en az şunları
ayırt eder: kaynak commit, hedef dosya hashleri, test/CI/inceleme sonuçları,
etkinleştirme yetkisi, gerçek olay kanıtı, oluşan kaynak ve çözülmemiş sınır.
Sentetik testlerde geçen senaryoların canlı hanesi otomatik doldurulmaz.
