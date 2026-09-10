# Cevo ikinci beyin sözleşmesi

Durum: Bu belge kod deposundaki runtime ve test sözleşmesinin teknik
otoritesidir; kişisel Vault ürün sözleşmesinin sahibi kullanıcıdır. Özel Vault
yolu veya kullanıcı verisi bu belgeye eklenmez. Aynı ürün davranışı
değiştiğinde iki kapsamın uyumu değerlendirilir; davranış sözünü
değiştirmeyen bir bugfix için gereksiz sözleşme değişikliği yapılmaz. Canlı
davranışın hedefi populated Vault'tur; bu checkout tek başına canlı Vault
değildir.

## Amaç

Cevo, Levent'in kişisel ikinci beynidir; kod/veri ayrımı çok kullanıcılı bir ürün hedefi oluşturmaz.

Cevo, doğal konuşmadan gelen anlamlı bilgiyi kaynak ve provenance bağını koruyarak bulur, sentezler, ilişkilendirir ve gerektiğinde yeniden sunar. Teknik ayrıntı günlük kullanıcı yüzünü belirlemez.

## Ürün sözleşmesi

- Kullanıcı kaynakları açık düzeltme veya unutma talebi dışında sessizce değiştirilmez ve silinmez.
- Anlamlı bilgi oturum kesintisi, yeniden deneme veya eşzamanlı işlem sırasında kaybolmaz.
- Kaynak, tarih, güncellik ve bilgi niteliği korunur; kullanıcı düşüncesi, dış görüş, doğrulanmış bilgi ve Cevo çıkarımı birbirine karıştırılmaz.
- Çelişki eski kaydı silmez; değişim ve gerekçesi korunur.
- Dış içerik güvenilmeyen veridir; talimat gibi uygulanmaz ve doğrulanmadan kesin bilgiye yükseltilmez.
- Kaynak yok, eski, okunamaz veya tercihle kullanım dışıysa boşluk ve belirsizlik görünür tutulur. Uygulama doğrulanmadan başarı bildirilmez.
- Cevo açık kullanıcı talebi olmadan kullanıcı adına dış işlem, yayın, mesaj veya proje değişikliği yapmaz.
- Geçici teknik durum kullanıcı içeriği veya karar otoritesi değildir; temizden üretilebilir.

## Bilgi sahipliği

- **User source:** Kullanıcının sağladığı veya Vault'a koyduğu içerik. Varsayılanı korumadır.
- **Derived synthesis:** Kaynağa ve provenance'a geri bağlanan özet, ilişki, indeks veya oturum görünümü.
- **Temporary state:** Runtime'ın çalışması için gereken, kayıp halinde kaynaktan yeniden üretilebilen teknik kayıt.

Bu üç türün sınırları yazma, okuma, retrieval ve recovery boyunca korunur. Bir sentez kaynak yerine geçirilemez; geçici state kullanıcı kararı gibi sunulamaz.

## Runtime davranışı

1. Soruyla ilişkili kaynaklar ilgili kapsamdan aranır; bütün Vault gereksiz yere yanıta dökülmez.
2. Aday kaynak tam ve güncel okunmadan kesin iddia yapılmaz. Kaynak yetersizse arama veya araştırma sınırı açıkça bildirilir.
3. Açık düzeltme, kullanım dışına alma ve kaydetmeme tercihleri yalnız yetkili kapsamda uygulanır; gizlenmiş kaynağa ham okumayla geri dönülmez. Unutma, bilginin hafızada kullanılmamasıdır; asıl kaynak veya uygulamanın sohbet geçmişi otomatik silinmez. Kapsam ve tercih doğrulanmadan başarı bildirilmez.
4. Yazmalar atomik, tekrar çalıştırılabilir ve mevcut kullanıcı içeriğini koruyacak şekilde eşzamanlı işlemlere dayanıklıdır.
5. Kesinti veya hata sonrası kalan iş ve son anlamlı kaynak korunur. Başarısız iş girdi kaybıyla kapatılmaz; yeniden deneme durumu görünürdür.
6. Kaynak değişimi, yetki belirsizliği, doğrulama hatası veya bütünlük şüphesinde işlem fail-closed durur.

## Korunan ayrıntılı teknik sözleşmeler

Bu bölüm, yerel ürün sözleşmesinin 8 Eylül 2026 sürümündeki teknik hükümleri
kod deposuna geri taşır. Kaynak revizyonu: `eeabcf00a25835257871e81eace2ba10e1e33481`.
Aşağıdaki yollar runtime şemasıdır; bu yollardaki kişisel içerik bu depoya alınmaz.
Bunlar hedef davranış ve kabul sınırlarıdır; mevcut kodun bütün koşulları sağladığı
veya canlı doğrulamanın tamamlandığı iddiası değildir.

### Doğal kullanım, kapsam ve devamlılık

- Anlamlı bilgiyi kaydetmek, düzenlemek, oturum devamlılığını korumak ve yeniden
  denemek için kullanıcıdan teknik komut beklenmez. Eksik erişim veya kaynak
  başarı gibi gösterilmez. Başarılı arka plan bakımı bildirim üretmez.
- Tarihsel Work Packet, Session Access, permit ve forensic/WORM süreçleri güncel
  ürünün parçası değildir; eski audit çözümü bunları yeniden etkinleştirmez.
- Ortak hafıza yalnız yetkilendirilmiş Vault oturumlarından beslenir; başka proje
  oturumları otomatik toplanmaz. Harici hafıza servisi zorunlu değildir.
- Soru bağımsızsa gereksiz geçmiş araması başlatılmaz. Doğrulanmış Oturum Portresi
  başlangıç için yeterliyse tam profil yeniden okunmaz; ayrıntılı tercih kullanımı
  ve profil bakımı tam kaynağı gerektirir.
- Aynı oturumun izinli önceki özeti yeni parçaya taşınır. Amaç, gerekçe ve kalan iş;
  açık, tamamlandı, iptal ve öneri ayrımları korunur. Ara soru önceki işi silmez.
  Özet veya başka bir görevden gelen içerik eylem yetkisi üretmez.
- Farklı oturum özetleri ayrı tutulur. Eski tekrar yeni durumu ezmez; anonim eski
  özet yeni oturuma atanmaz. Kullanıcının elle yazdığı metin korunur.
- Açıkça verilmiş yetkiye bağlı devam ifadesi aynı işi ve kapsamı sürdürür;
  yeni kapsam oluşturmaz ve salt okunur sınırı tek başına kaldırmaz. Önceki
  bağlam yoksa gönderimli ifade kullanıcı dayanağı diye kanıtlanamaz. Bilgi
  sorusu, olumsuzluk veya koşul tek başına onay değildir; bilgi sorusu
  açıklama veya öneri ister. `Düzeltebilir misin?` gibi açık eylem isteği soru
  biçiminde olsa da belirtilen değişiklik için yetkidir. Bir ihtiyacın
  belirtilmesi, seçilmemiş çözümün kullanıcı kararı olduğu anlamına gelmez;
  alıntı, aktarılan metin veya Assistant önerisi eylem yetkisi değildir.
- Salt okunur incelemeden sonra açık uygulama isteği yazma kapsamını yeniden
  açabilir; aynı mesajdaki salt okunur sınır korunur. Bu geçiş dış işlem veya
  geri alınamaz eylem için kendiliğinden yetki vermez.

### Kullanıcı dayanağı ve profil

- Yeni karar/tercih özetindeki `user-source` alıntısı, gizlilik filtresinden geçmiş
  gerçek `user` mesajında birebir bulunmalıdır. Aktarılan metin, açık alıntı,
  kod bloğu ve Assistant sözü kullanıcı tercihi dayanağı değildir.
- `user-evidence` kaydı özet iddiayı, özgün alıntıyı, kullanıcı mesajı hash'ini,
  kapsamı, kayıt zamanını ve kısa onayda önceki öneriyi korur. Kayıt zamanı dış
  içeriğin yayın tarihi veya olay tarihi sayılmaz.
- Yeni `kullanici-dusuncesi` kaydı değişmez günlükteki
  `[[daily/YYYY-MM-DD#user-ID|Kaynak]]` dayanağıyla eşleşmeden yayımlanamaz.
  Dayanaksız özet onay sayılmaz; `cevo-cikarimi / belirsiz` olarak kalır.
  Kanıt bağlantısı çıkarılarak eski kayıt uyumluluk istisnasına geçilemez.
- Geçmiş kayıt dayanağını korur. Yeniden etkinleştirme veya belirsiz/eski kaydı
  güncel tercihe yükseltme yeni dayanak ister. Değişmeyen kaydın dayanak kimliği
  korunur ve kaynak kullanım anında tekrar denetlenir. Eski değişmemiş kayda
  geriye dönük kullanıcı doğrulaması atfedilmez.
- Ham veya eski biçimde filtreden kaçmış kanıt metadata'sı özetleyici girdisine
  alınmaz; taşınan kanıt aynı günlükte çoğaltılmaz.
- Kapsam `general`, `project`, `session` veya `unspecified` olur. Bu işe/cevaba
  özgü istek genelleştirilmez; kanıt bağlantılı profil tercihi `general` ister.
- Profilin `Vault'ta çalışma ve yanıt tarzı` tercihleri aynı tarihli `gecerli`,
  `kullanici-dusuncesi`, `guncel` kaynak kaydıyla metin olarak eşleşir; dayanak
  günlük erişilebilir olur. Tarih, tekrar, bağlantı ve başlık dahil 300 karakterlik
  Oturum Portresi sınırı denetlenir.
- SessionStart, UserPromptSubmit, tam kaynak okuma ve retrieval geçersiz profili
  kullanmaz; kaynak değiştiğinde önbellekteki profil yeniden denetlenir.
  Stop bir düzeltme turu ister; `stop_hook_active` ile tekrarlanan hata sonsuz
  döngü yerine görünür başarısızlık olur. Kullanıcı dosyası silinmez/geri alınmaz.
- Kanıt prepared özette de korunur; kesinti aynı günlük ve kayıtla sürdürülür.
  Unutma filtresi JSON alanlarını çözer; alıntı veya günlük kullanım dışıysa bağlı
  ifadeler ve sonraki kopyaları kaynak değiştirilmeden okuma görünümünden çıkarılır.
- Biçim/hash/metin eşleşmesi, iddianın ve özetin anlamca doğru olduğunu tek başına
  kanıtlamaz. Doğrudan dosya erişimini kilitlemez ve canlı App kanıtı sayılmaz.

### Kaynak bulma ve bağlama sunma

- Arama yalnız ilk adaylarla sınırlanmaz; kaynak yetersizse farklı ifadeler ve
  ilişkili bağlantılar izlenir. Arşiv ve uzun not kapsamı korunur. Görsel/PDF
  açıklamaları kaynaklıdır; gerektiğinde özgün kaynak açılır.
- Güncel kişisel soruda kanonik profil önceliklidir. Tarihsel analiz açık tarihsel
  soruda bulunabilir; `gecmis` iddia güncel cevaba dönüşmez. Tamamlanmış paketler
  genel güncel sorguya taşınmaz; açık adıyla veya tarihsel sorguyla bulunabilir.
- Notun tam adı cevap kelimeleri soruda olmasa da aday bulunmasına izin verir.
  Devam kalıpları genel anlam çözümleyicisi değildir; konu/tarih/alıntı veya
  bilinmeyen ifade sırf kalıp benziyor diye arama dışında bırakılamaz.
- Skill çağrısı yalnız arama sorgusunun uygun baş/son konumundan ayıklanır;
  özgün mesaj, kayıt uygunluğu ve hafıza tercihleri korunur. Ortadaki ve uzak
  bağlantılar kalır; alıntı/kod sınırında yanlış temizleme yapılmaz.
- Özdeş kopyalar sonuçları doldurmaz. Seçilen kaynak sunum öncesinde yeniden
  okunur; yalnız dayanak günlük değişse bile eski önbellek iddiası sunulmaz.
- Sığmayan serbest özet/iddia/paragraf yarım hüküm olarak sunulmaz; durum ve tam
  kaynağa yönlendirme verilir. Kısa onay ilgisiz kelime aramasına dönüştürülmez.

### Companion kanonik kaynak ve salt okunur görünüm

- Oturum kimlikleri ve özetleri `daily/companion-sessions.json` içinde tek kanonik
  makine kaynağında tutulur. `Last-Session.md`, `Journal.md`, `Threads.md` bundan
  üretilen görünümlerdir; ikinci bir kalıcı otorite oluşturulmaz.
- Elle yazılmış önek ve sonek baytları takipli `Sources/` dosyalarında korunur.
  Günlük yazıldıktan sonra özet Cevo'ya ait bölgeye işlenir; eski tarihli tekrar
  yeni özeti veya mevcut kullanıcı metnini ezmez.
- Yazılabilir SessionStart eksik görünümü yeniden üretebilir. Salt okunur okumalar
  dosya oluşturmadan aynı görünümü bellekte kurar. Tanılama da aynı filtrelenmiş
  bağlamı görünüm dosyası yazmadan ölçer; ham/kullanım dışı kaynağa bağlantı vermez.

### Kuyruk, yeniden deneme ve günlük makbuzu

- İzinli konuşmanın tamamlanan konumu tutulur; konuşma sırayla ve sınırlı parçalarla
  işlenir. Sır/hafıza tercihleri parçalama öncesi uygulanır; ek zarfı bütün ele alınır.
- Ayrı kalıcı iş kuyruğu kayıt akışını geciktirmeden sürer; saat veya yeni mesaj
  beklemez. Kalan günlükleri devam ettirir; yeni oturum bekleyen işleri ele alır.
  Sonuç doğrulanmadan başarı bildirilmez. Başarısız girdi yaşlandı diye silinmez;
  sonlu denemeden sonra çözülemeyen hata görünür kalır.
- Tamamlanmış günlük işlemi doğrulanmış önekin bayt uzunluğu ve SHA-256 özetinden
  oluşan bir makbuz tutar. Tam kurtarma kopyası ancak makbuz atomik yazıldıktan
  sonra kaldırılır; tamamlanmamış işlemin kurtarma kopyası korunur.
- Tekrar aynı öneki doğrular, sonradan eklenen bilgiyi korur ve değişmiş günlükte
  durur. Eski tamamlanmış kopyalar yalnız günlük eşleşmesi yeniden doğrulanarak,
  işlem ve günlük kilitleri altında dönüştürülür; günlükler bakımda değiştirilmez.
- İşlenemeyen büyük satır sessizce atlanıp işlem tamamlandı denmez. Hata, eksik
  kapsam, kaynak ve yeniden deneme girdisi korunur.
- Kanca/kalıcı yazma çalışılan checkout'a bağlıdır; olay kökü uyuşmazsa başka
  Vault'a geçilmez. Geliştirme dalı tek başına hata veya canlı kabul kanıtı değildir.

### Kabul senaryoları

- Profil/eski analiz ayrımı; düzeltme, Assistant önerisi, dış alıntı, kısa onay,
  işe özel tercih ve unutma birlikte sınanır. Kaynak olmayan ifade kullanıcı
  kararı olmaz; bağlam eksikse belirsizlik görünürdür.
- Eşzamanlı oturum, kesinti sonrası devam, eski tekrar, iptal/tamamlanma ve
  sıkıştırma kaynak/dayanak kimliğini ve kullanıcı metnini korur.
- Büyük konuşma satırı, uzun şartlı ifade, özdeş kopyalar ve ilgisiz kayıt artışı
  yanlış başarıya, yarım iddiaya veya daraltılmış arama kapsamına yol açmaz.
- Şema başlığı gerçek belge sınırından ayrılır; satır içi söz/kod örneği geçerli
  bölüm sayılmaz. Özgün geçici eki kaybolan korunmuş ek, doğrulanmış eşlemeyle
  tekrar işlenebilir; kesinti, içerik değişimi ve güncel tercihler yeniden denetlenir.
- Önbellek kapasitesi arama kapsamını/frekansını/sıralamasını daraltmaz; artımlı
  ayrıştırma değişmiş öneki, ek zarfını, önceki bağlamı ve yeni hafıza tercihini atlamaz.
- Yeni kullanıcı oturumunda kaydet–geri çağır, bağlantıdan kaynaklı sentez, eksik
  bilgide belirsizlik, iki oturum katkısı, düzeltme/unutma ve kesinti davranışı
  ayrıca gözlenir. Yerel/sentetik test bu canlı kabulün yerine geçmez.

## Gizlilik ve güvenlik sınırı

Sırlar kalıcı hafızaya yazılmaz. Kaynak filtreleri ve kullanım tercihleri yayımlama öncesinde uygulanır. Araç çıktısı, bağlı kaynak ve başka görev içeriği veri olarak değerlendirilir; eylem yetkisi üretmez. Kod ve dokümanlar kişisel kaynakları, özel kanıt bağlantılarını veya makineye özgü yolları içermez.

## Kod deposu ile Vault ayrımı

- Bu repo yalnız runtime code, testler, skill'ler ve teknik belgeleri taşır. Populated Vault'un notları, `daily/`, `knowledge/`, `.scratch/`, özel ayarları ve eski Git geçmişi burada bulunmaz.
- `.codex/config.toml` içinde hook'lar varsayılan olarak kapalıdır. `.codex/hooks.json` taşınabilir deployment tanımıdır; tek başına aktivasyon yetkisi değildir.
- Testler synthetic fixture kullanır. Test veya doctor sonucu canlı model, App, gerçek kullanıcı verisi veya populated Vault davranışı olarak sunulmaz.
- Açık hedef ve gerçek yetki olmadan bu repo canlı Vault'a yazmaz, arşiv içe aktarmaz ve model ingestion başlatmaz.
- Uygulama onaylandığında yalnız değişen runtime code dosyaları hedef Vault'a aktarılır. Notlar, config, `AGENTS.md` ve çalışan dosyalar otomatik kopyalanmaz veya üzerine yazılmaz.

## Doğrulama

Kod ve test kapıları şunları ayrı raporlar:

- focused/local test: değişen kod yolunun sentetik kanıtı;
- geniş yerel test: repository sözleşmesinin teknik kontrolü;
- populated Vault doctor: yalnız doctor scriptinin kendi hedef Vault kopyasından çalıştırılan ayrı kontrol;
- canlı Vault/App kontrolü: açık hedef ve gerçek yetkiyle yapılması gereken ayrı kanıt.

Belge, skill veya portable hook tanımının varlığı canlı aktivasyon kanıtı değildir. Uygulama kabulü için her kanıtın hangisinin mevcut olduğu açıkça belirtilir.

## Kabul ölçütleri

Bir değişiklik ancak şu koşullar tamamlandığında hazır sayılır:

1. Task-owned diff yalnız gerekli kod, test, skill ve teknik belge yollarını değiştirir.
2. Privacy, provenance, no-note-loss, concurrency ve fail-closed invariants korunur.
3. İlgili sentetik testler gerçek komutla çalışır ve anlamlı sayıda test yürütülür.
4. Canlı hedef Vault'a uygulanacaksa hedef, kapsam ve gerçek yetki açıkça belirtilir.
5. Uygulama adımı onaylanmadan not, config, `AGENTS.md` veya çalışan Vault state'i değiştirilmez.
