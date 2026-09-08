# Audit bulgularının uygulama paketleri

Tarih: 8 Eylül 2026. Durum: planlandı; runtime uygulaması yapılmadı.

Bu kayıt, PR #1/#2 içerik incelemesindeki geçerli adayları eski auditlerle eşler.
Bütün geçmiş auditlerin kapandığı veya bütün bulguların burada toplandığı iddiası değildir.
Başlangıç runtime snapshot'ı `1d5af84`; teknik sözleşme düzeltmesi `0f97f3d`.
Paket başlangıcında güncel kaynak ve daha önce yapılmış düzeltmeler yeniden karşılaştırılır.

## Eşleme ve sıra

| Paket | Kapsam | Eski audit ilişkisi | Karar / bağımlılık |
|---|---|---|---|
| P01 | Timeout sonrası süreç ağacının sonlanması | Concurrency C06 / T07; PR #1 process cleanup bulgusu; PR #2 Windows CI hatası | Önce teşhis. Kalıcı orphan ile tamamlanması beklenmeyen sonlandırmayı ayır. |
| P02 | Önekli credential adlarının filtrelenmesi | BUG-007 tırnaklı JSON credential ve BUG-017 compiler redaction aynı gizlilik sınırında; yeni önek senaryosu bunlarla aynı hata değildir | P1 öncelik. P01'den bağımsız; ayrı dosya/test sahipliğiyle paralel olabilir. |
| P03 | Supervisor/worker süreç kimliği ve kurtarma | Concurrency C01/C02/C06, T02/T03/T07; PR #1/#2 PID bulguları | P01 sahiplik/cleanup sözleşmesinden sonra. |
| P04 | Yayın sırasında hedef değişimi ve kısmi sonuç | Concurrency C05a/C05b, T05/T06; PR #1/#2 promotion bulguları | Aynı dosyalardaki eski adayları uzlaştır; P03 ile worker kurtarma sınırı ortaksa sıralı uygula. |

Önce P01 ile CI engelinin nedenini belirle; P02 gizlilik açığını ayrı tut.
Ardından P03 ve P04 gelir. Bunlar baştan dört büyük refactor yapılması talimatı değildir.
Her pakette yalnız güncel ve gösterilebilir açık düzeltilir; zaten doğru çalışan eski
çözüm yeniden yazılmaz. Ortak dosyada tek yazar kullanılır.

## Eski kanıtlar ve yeniden kullanılabilecek adaylar

Aşağıdaki kimlikler yerel teknik audit kayıtlarıdır; kaynak raporlar ve eski Vault
Git geçmişi bu kod deposuna kopyalanmadı. Commit'ler bu ayrı GitHub deposunda
çözümlenmeyebilir. Yerel kaynak tarihlerinin ve adayların bugünkü durumunun
uygulama başlangıcında yeniden doğrulanması gerekir.

| Paket | Somut eski kayıt | Eski düzeltme / yeni açık ayrımı |
|---|---|---|
| P01 | 6 Eylül flake audit F2; concurrency C06/T07; architecture MP-24 | F2 başka bir süreç testindeki child readiness kırılganlığıdır; bugünkü wrapper sonrası canlı PID assertion'ıyla aynı repro değildir. Eski R01/R02/R11 substrate işleri `9aff1026` ve `737faa63` ile Vault HEAD geçmişinde bulunur; tekrar uygulanmaz. |
| P02 | 8 Eylül repo audit BUG-007 ve BUG-017; architecture MP-15 yakın konu | BUG-007'nin JSON anahtar düzeltmesi `45be0bca` Vault HEAD geçmişinde. Yeni env-prefix örneği için birebir eski bulgu/çözüm kanıtı yok; ayrı alt senaryo olarak RED gerekir. BUG-017 model staging/repair gizlilik sınırını koruyan mevcut regresyondur. |
| P03 | Architecture MP-04; concurrency C01/C02/C06, T02/T03/T07 | MP-04 lease yüzünden gerçek sahibin ezilmesiyle ilgilidir; bugün ilgisiz canlı PID yüzünden takılma bunun ters yöndeki riski. C02 claim adayı `56ba6919`, C06 süreç adayı `392b0091` Vault HEAD atası değildir; tüm sonraki ownership düzeltmeleriyle birlikte değerlendirilmeli. |
| P04 | 7 Eylül Python audit F01; architecture MP-14 (MP-11/13 ilişkili); concurrency C05/T05/T06 | F01 kopya/baseline sırası düzeltmesi `4baf0c27` Vault HEAD geçmişinde; bugün son doğrulama/replace aralığı ayrı. Replace/journal adayları `21b04a6d`, `ef52433f` Vault HEAD atası değildir. 8 Eylül repo raporundaki başarılı retry karşı kanıtı korunur. |

Sınırlı kaynak okuması: `python-review-20260907/rapor.md` F01/F07;
`repo-bug-audit-20260908/report.md` BUG-007/017 ve adayları eleme bölümü;
`concurrency-audit-remediation-20260908/spec.md` T02/T03/T05/T06/T07;
`flake-audit-20260906-final/report.md` F2 ve
`cevdet-architecture-hardening-20260903/finding-inventory.md` ilgili MP kayıtları.
Bunlar yerel `.scratch` teknik arşiv adlarıdır; eski raporların tamamı okunmuş sayılmaz.

Ayrı concurrency adayı `codex/concurrency-remediation-20260908` dalında,
`b800f337b8050d9f232d4b9a1146dd91d4da2a4d` uç commit'inde hâlâ mevcut.
Önce ilgili hunk'ları ve sonraki güvenlik düzeltmelerini güncel kodla karşılaştır.
İlk ara commit'i tek başına almak sonraki fail-closed düzeltmelerini kaybettirebilir.
Bu dalın eski test sonuçları güncel code-only/CI veya canlı App kabulü değildir.

Python audit F07'nin Windows ctime/cache konusu daha önce content-hash yönünde
çalışılmıştır. PR'daki POSIX ctime fixture iddiası bunun aynısı değildir; yeni platform
kanıtı olmadan eski F07'yi yeniden açık runtime hatası sayma.

## P01 — Süreç sonlandırma ve CI

- Yüzey: `.codex/scripts/process_control.py`, `.codex/scripts/codex_runner.py`, ilgili
  `test_codex_brain.py` süreç testleri ve gerekirse ayrı küçük regresyon dosyası.
- Kanıt: Windows CI `34263151056` koşusunda 716 testten yalnız
  `test_timeout_kills_descendant_after_wrapper_exits` başarısız. Aynı assertion önceki
  koşuda da başarısız; yerel 716 test geçmişti. Bunlar tarihli snapshot sonuçlarıdır.
- İlk iş: wrapper çıktıktan sonra child PID'nin durumunu bounded biçimde gözle;
  cleanup talebi, cleanup tamamlanması ve doğrulanamayan cleanup ayrı sonuç olsun.
  Testi atlama, koşulsuz sleep ekleyip başarı ilan etme, PID yaşamını yok sayma.
- Kabul: gerçekten sona eren descendant doğrulanır; doğrulanamayan süreç sıradan
  güvenli timeout/retry sayılmaz. Sadece testin başlattığı sentetik süreçler kullanılır.
- POSIX `poll()` erken dönüşü aynı fonksiyondaki ayrı kod yoludur. Düzeltmesi seçilirse
  gerçek POSIX koşusunda kanıt gerekir; Windows başarısı POSIX kanıtı değildir.

## P02 — Sır filtresi

- Yüzey: `.codex/scripts/memory_ledger.py` ortak sınıflandırma/sanitizer sınırı ve
  mevcut transcript, attachment, compiler çağrıları. Her çağrıya ayrı regex ekleme.
- Sentetik örnekler: `DATABASE_PASSWORD=synthetic`, `MY_TOKEN=synthetic`,
  `AWS_SECRET_ACCESS_KEY=synthetic`. Gerçek credential kullanma veya loglama.
- Önce `contains_secret` ile `sanitize_text` için aynı girdi sözleşmesini doğrula;
  normal metinde yanlış pozitifleri, tırnaklı/çok satırlı değerleri ve mevcut
  BUG-007/BUG-017 davranışını koru. Ardından model girdisi ve kalıcı çıktı yolunu sına.
- Kabul: desteklenen credential adları ortak sınırda temizlenir; değerler indeks,
  model girdisi veya kalıcı senteze kaçmaz. Bu bulgu gerçek bir sırrın sızdığı kanıtı değildir.

## P03 — Süreç sahipliği

- Yüzey: `.codex/scripts/worker_supervisor.py`, gerektiği ölçüde process identity
  yardımcısı ve `test_worker_handoff.py` / `test_worker_result_contract.py`.
- Canlı PID aynı supervisor/worker örneği demek değildir. Ölüm, yeniden kullanılan PID,
  geç başlayan child, eski claim token ve uzun süren gerçek owner ayrı sınanır.
- Lease doldu diye kör yeniden başlatma/requeue yapma; gerçek owner kimliği veya
  sahiplik kilidi doğrulanmadan ikinci yazıcı yaratma.
- Kabul: ilgisiz yeniden kullanılmış PID işi sonsuza dek bloke etmez; hâlâ çalışan
  gerçek owner çoğaltılmaz. Eski child yeni claim'i benimseyemez. P01 cleanup
  doğrulanamıyorsa bu sınır recovery/adoption tarafından atlanamaz.

## P04 — Yayın ve kurtarma

- Yüzey: `.codex/scripts/compile.py`, gerekirse `compile_state.py` / mevcut atomik
  yazma yardımcısı; `test_python_audit_regressions.py`, compile recovery testleri.
- Önce hedefi hash kontrolü ile replace arasındaki noktada değiştiren deterministik
  senaryo kur. Yalnız kontrolü replace'e yaklaştırmak atomik karşılaştırma garantisi değildir.
- İkinci hedefte hata/kesinti ve yeniden çalıştırma senaryosunda hangi dosyaların görünür
  kaldığını, cursor/receipt/stage durumunu, değişen hedefi ve güncel unutma tercihini denetle.
- Karşı kanıt: eski repo audit raporu ikinci promotion hatasından sonra geçici karışık
  sonuç görmüş fakat gerçek retry'nin toparladığını belirtmişti. Mevcut garanti dosya
  başına atomikliktir; bütün ağaç transaction gereksinimini bu tespit tek başına doğurmaz.
- Kabul: başka yazıcının değişikliği sessizce ezilmez; yarım sonuç sağlıklı tamamlanmış
  işlem diye sunulmaz. Gerekli recovery girdisi kaybolmaz; retry kullanıcı değişikliğini
  geri alan kör rollback yapmaz. Çözüm mevcut staging/digest/kilitleri temel alır;
  yeni genel transaction framework'ü varsayılmaz.

## Bu paketlere alınmayanlar

- Sabit Levent kimliğini çok kullanıcılı ürüne dönüştürme: Cevo'nun onaylı kişisel
  kapsamına aykırı gereksiz genelleştirme; bot bulgusu bu kapsamda reddedildi.
- POSIX `ctime` fixture beklentisi: Windows CI hatasından ayrı taşınabilirlik konusu.
  POSIX doğrulaması seçildiğinde ayrıca ele alınır; mevcut Windows testini açıklamaz.
- Eski auditlerdeki bütün diğer maddeler: bu eşleme onların güncel durumunu veya
  uygulama yetkisini belirlemez. Kişisel/global/korpus testlerinin code-only depodan
  çıkarılması runtime testlerinin keyfi kaldırılması diye yorumlanmaz.

## Uygulama ve kanıt sınırı

Paketler [PR akışına](pr-workflow.md) göre ayrı branch/worktree ve PR ile ilerler.
İlk aktarım/altyapı PR'ları merge edilmeden boş uzak main üzerinden geliştirme başlatılmaz.
Bağımlı branch zorunluysa tabanı açıkça yazılır ve merge sırası korunur.

Davranış değişikliğinde ilgili odaklı test ve tam sentetik paket çalıştırılır:
`python -B -X utf8 -m unittest discover -s .codex/tests -q`.
Yeni test dosyası explicit `.gitignore` izin listesine eklenir. Code-only checkout'ta
populated Vault doctor veya canlı model çalıştırılmaz. Canlı hedefe teslim, ayrı hedef,
kapsam ve kullanıcı onayıyla yapılır; local/CI başarısı canlı kabul değildir.

Eski worktree/commit yalnız aday kaynaktır. Güncel API ve regresyonlarla karşılaştırmadan
bütün eski branch'i cherry-pick/merge etme; Vault notu, arşiv veya `.scratch` verisini
GitHub'a taşıma. Bu belge uygulama tamamlandı/merge onayı kaydı değildir.
