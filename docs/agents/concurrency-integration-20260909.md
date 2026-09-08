# Concurrency düzeltmelerinin güncel kodla entegrasyonu

Kaynak aday: `b800f337b8050d9f232d4b9a1146dd91d4da2a4d`.
Eski aday tabanı: `d536643c0dc55f8c64c33293f68cbdb1addf8748`.
Güncel code-only taban: `f89674a6bfdb9561557074f11c43cf3846813e9c`.
Tarih: 9 Eylül 2026. Merge durumu ilgili GitHub PR'ının güncel durumundan okunur.

## Aktarım sınırı

Yalnız kod/test değişiklikleri üç yönlü karşılaştırmayla taşındı. Eski Vault Git
geçmişi, notları, daily/knowledge verileri, runtime state ve özel ayarlar taşınmadı.
11 çakışmalı dosyada güncel gizlilik, kaynak okuma, code-only test ve scope
korumaları tutuldu. Kullanıcının tercihi gereği `.codex/config.toml` hooks=false
kalır; kod merge edilmesi canlı hook veya model etkinleştirmesi değildir.

## 13 bulgunun karşılığı

| Bulgu | Güncel karşılık / sınır | Başlıca regresyon yüzeyi |
|---|---|---|
| C01 | Supervisor admission/devir ve sahipsiz pending koruması | test_concurrency_queue.py, test_worker_handoff.py |
| C02 | Başlangıç claim token'ı ve süreç kimliği; eski child yeni işi alamaz | test_concurrency_queue.py |
| C03 | Hook input referans kontrolü ve temizliği aynı queue kilidinde | test_concurrency_queue.py |
| C04 | Ayrı Git index snapshot'ı ve expected-parent ref kontrolü | test_concurrency_checkpoint.py |
| C05 | Windows sharing retry, journal/digest kurtarma ve publication reader fence | test_concurrency_replace.py, test_concurrency_publication.py, test_concurrency_publication_retry.py |
| C06 | Atomik Windows Job sahipliği, bounded completion, süreç kimliği, kalıcı cleanup fence | test_concurrency_process.py, test_concurrency_worker_cleanup.py, test_concurrency_compile_cleanup.py |
| C07 | Coalesced pending ihtiyaç ve unresolved queue kabul sınırı | test_concurrency_queue.py |
| C08 | Sınırlı terminal receipt retention ve gereksiz per-job sidecar üretiminin kesilmesi | test_concurrency_queue.py |
| C09 | Okumadan önce alınan session/generation token; yeni istek ve incomplete tail korunur | test_concurrency_reflection.py |
| C10 | Güncel LineSpan/index EOF sözleşmesi korundu; tamamlanan önek ilerler, eksik tail hata/pending kalır | test_concurrency_transcript.py, test_flush_completion.py |
| C11 | Entry, projected içerik, ham hash ve stat aynı bounded snapshot'ta; karışık cache nesli yok | test_concurrency_readers.py, test_codex_brain.py |
| C12 | Git deadline/diagnostic hata izolasyonu ve publication durumunun görünürlüğü | test_concurrency_readers.py, test_doctor_failure_isolation.py |
| C13 | Daha yeni metadata-only transcript_index yolu korundu; kullanılmayan ikinci spool motoru taşınmadı | test_transcript_index.py, test_concurrency_transcript.py |

C13 için sabit toplam bellek iddiası verilmez: tam transcript metni indekste tutulmaz,
seçilen metin parçaları sınırlıdır; satır/chunk metadata'sı kayıt sayısıyla büyür.
Eski spool helper'ını test edip üretim yoluna ait bellek kanıtı gibi sunmak yerine
aktif indeks yolu sınandı. Metadata büyümesine ayrıca sınır koymak bu aktarımda yapılmadı.

## Entegrasyon incelemesinde giderilen ek boşluklar

- Eski sıkı wrapper-exit testi Job Object active_processes=0 iken kısa süre daha
  canlı PID görebiliyordu. Bounded sonlanma sinyali bekleniyor; sabit sleep ile
  assertion gizlenmedi. PID readiness fixture'ı dosyanın tam yazılmasını bekler.
- Normal owned Windows launch, CI gibi breakaway izni vermeyen host job içinde
  nested Job Object olarak açılır; atomik sahiplik korunur. Yalnız açık
  `spawn_detached` çağrısı breakaway ister. Gerçek kısıtlı host fixture
  başlangıçta AccessDenied verdi; düzeltmeden sonra süreç paketi geçti.
- İç model cleanup hatası tuple/genel OSError yolunda sıradan retry'ye dönüşmüyor.
  Worker lane fence'i ve child fence'in parent'a taşınması korundu. Doğrudan
  compiler CLI yolu maintenance lane'ini durdurur; belirsiz süreçte stage korunur.
- İlk replace ve her sharing retry öncesi aynı beklenen hedef hash'i doğrulanır.
  Promotion/recovery sırasında araya giren yazıcının verisi korunur; pending
  journal/stage durur. Bu, dosya sistemi atomik compare-and-swap garantisi değildir.
- Arama helper'ları arasında kaynak değişimi aynı doğrulama döngüsüne alındı.
- Reflection token transcript indekslenmeden önce yakalanır; incomplete EOF varsa
  tamamlanmış prefix coverage yazılsa bile acknowledgment yapılmaz.
- Güncel hook scope doğrulaması gevşetilmedi; sentetik SessionStart fixture'ına
  gerçek geçici Git kökü ve eşleşen cwd eklendi. Eski suspended-launch mock testi,
  yeni atomik native launch'ın hata/handle temizliği sözleşmesine uyarlandı.

- Filtrelenmiş view wrapper'ları publication hatasının türünü korur.
- FLUSH_BOS receipt'i batch sınırlarını saklar; receipt sonrası coverage kesintisinde
  aynı doğrulanmış boş sonuç tekrar modele gönderilmez.

## Doğrulama

- Eski dalın 666 test sonucu tarihsel kanıttır; güncel code-only sonuç değildir.
- İlk birleşik 861 test koşusu 8 failure/4 error verdi. Uyarlamalar ve bulunan
  yarışlar giderildi; ara birleşik koşuda 863 test, son yerel Windows koşusunda
  869 test (91.491 saniye, OK) geçti. GitHub CI sonucu ayrıca raporlanır.
- Yayın retry regresyonları eski b800 adayında 2 RED, düzeltmede 2 GREEN verdi.
- Süreç cleanup, publication ve reader/reflection incelemelerinde bildirilen
  maddi bulgular kaynak düzeltmeleri ve odaklı testlerle giderildi.
- Windows tam komut: `python -B -X utf8 -m unittest discover -s .codex/tests -q`.
- Teslim kapsamı Windows'tur. Kullanıcının kapsam düzeltmesiyle ek Linux CI işi
  kaldırıldı; gerçek POSIX için tam doğrulama iddiası verilmez.
- Code-only checkout'ta populated Vault doctor çalıştırılmadı. Eski iki doctor
  FAIL'i yeni çalışma için PASS veya güncel sağlık sonucu sayılmaz. Canlı App,
  gerçek model ve fiziksel güç kesintisi doğrulanmadı; hook'lar açılmadı.

## Açık kalan ayrı işler

[Audit paketleri](audit-packages-20260908.md) içindeki P02 env-prefix redaction bu
concurrency aktarımına dahil değildir. P04'ün keyfi harici yazıcıya karşı son
kontrol/replace aralığında atomik CAS beklentisi de tamamen kapanmış sayılmaz.
Bu iki başlık, mevcut iyileştirmelerin yeniden yazılmasını gerektiren yeni bir
13 maddelik görev listesi gibi ele alınmaz; güncel fark üzerinden ayrı yürütülür.
