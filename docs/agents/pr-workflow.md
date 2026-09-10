# CEVDET PR çalışma düzeni

Kod deposu: https://github.com/zPfizer/CEVDET-code (private).
Yerel Vault ve eski Vault Git geçmişi bu depodan ayrıdır.

## Her bağımsız iş için

1. Kod deposunda `git fetch origin` çalıştır. Güncel `origin/main` üzerinden ayrı `codex/<konu>` branch'i ve komşu bir worktree oluştur. Aynı worktree'de eşzamanlı görev çalıştırma.
2. Bir görev birkaç bağımsız değişiklik içeriyorsa her birini ayrı branch/PR'a ayır. Birbirine zorunlu bağımlı değişiklikler aynı PR'da olabilir.
3. Yalnız görev dosyalarını commit et; `git add .` ile ortak çalışmaları toplama. Yeni dosyayı mevcut `.gitignore` izin listesine açıkça ekle. Kod incelemesinde [Code Review Graph akışını](../../AGENTS.md#code-review-graph) uygula; PR karşılaştırmasında merge-base kullan. Graf, kaynak incelemesi ve testlerin yerine geçmez.
4. Odaklı testleri çalıştır. Davranış değişikliği varsa mevcut tam sentetik test paketini de çalıştır. CI Windows/Python 3.14 üzerinde aynı paketi çalıştırır.
5. Branch'i push et ve PR aç. Bitmemiş iş için draft kullan; incelemeye hazır olduğunda ready durumuna geçir. Hedef branch `main` olsun.
6. İlgili sözleşme ile diff'in uyumunu incele; Codex otomatik incelemesini ve CI sonucunu takip et. Yeni commit sonrasında önceki incelemeyi güncel kabul etme; son commit için yeniden inceleme al. Gerekirse PR yorumunda `@codex review` iste; yorum gönderme yetkisi görev kapsamında yoksa kullanıcıdan al.
7. Bulguları çöz ve gerekli testleri tekrar çalıştır. Kullanıcı onayından sonra squash merge yap; otomatik merge açma. GitHub koruması varsa yeşil kontroller ve çözülmüş konuşmalar aranır.
8. Sonraki bağımsız işi güncel `origin/main` üzerinden başlat. Worktree temiz ve kullanım dışı olmadan kaldırma; başka görevin branch'ini değiştirme.

## İlk aktarım

Kod aktarımı PR #1, PR altyapısı ise onun üzerine kurulan PR #2 içindedir. #2 önce `codex/initial-code-import` branch'ini hedefler; #1 merge edildikten sonra #2 tabanını `main` yap ve diff ile CI sonucunu yeniden doğrula. Squash merge nedeniyle #1 commit'i geçmişte korunmazsa #2'nin yalnız altyapı commit'lerini güncel `origin/main` üzerine rebase et. İlk aktarım tamamlanmadan bağımsız geliştirmeyi boş uzak `main` üzerinden başlatma.

## Yerel Vault'a teslim

PR merge edilmesi canlı Vault'u değiştirmez. Açık hedef ve uygulama yetkisiyle, onaylanan runtime diff'i yerel Vault'ta ayrı branch/worktree üzerinden denetlenir. Hedef dosyalardaki mevcut değişiklikler karşılaştırılır; kör klasör kopyası yapılmaz. Vault notları, günlükler, knowledge, özel ayarlar, config ve AGENTS otomatik aktarılmaz. İlgili test/doctor kontrollerinden sonra canlı uygulama ayrıca doğrulanır.

## İnceleme kurulumu

Code Review Graph kullanım seçimi ve sınırları için `AGENTS.md` içindeki üç akışı
izle. [9 Eylül ölçümü](graph-measurement-20260909.md), başlangıcı bilinen dar
sorguları kapsar; çok dosyalı etki incelemesinin genel fayda ölçümü değildir.
Grafın risk/test-boşluğu etiketleri inceleme adayıdır.

Codex ayarlarında bu depo için `Review my PRs` ve `On every push` etkinleştirilmiştir. Review kuralları kök `AGENTS.md` içindedir. Bunlar CI veya kullanıcı merge onayının yerine geçmez. GitHub hesap planı branch protection desteklemiyorsa sunucu tarafı zorunluluk varmış gibi raporlama.

Kaynak: https://learn.chatgpt.com/docs/third-party/github (8 Eylül 2026).

## PR Code Review Graph pilotu

`.github/workflows/pr-graph-report.yml`, yalnız kod, bağımlılık veya workflow
dosyası içeren pull request'lerde çalışır; yalnız belge değiştiren PR'larda
gereksiz graph işi başlatmaz. Job Windows üzerinde Python 3.14 kullanır,
PR'ın `head.sha` commit'ini `fetch-depth: 0` ile checkout eder, event'teki
`base.sha` commit'ini alır ve `git merge-base base.sha head.sha` sonucunu
karşılaştırma tabanı yapar.

Rapor işi `code-review-graph==2.3.8` ile native full build çalıştırır ve
`tools.detect_changes_func` çağrısını yalnız bu checkout üzerinde yapar; ürün
kodu import veya execute edilmez. Sonuç job summary ve artifact olarak
üretilir. Aynı repository içindeki PR'larda ayrı publisher job, yalnızca
`pull-requests: write` izniyle `<!-- cevdet-pr-graph-report -->` işaretli tek
bot yorumunu günceller. Fork PR'larında summary ve artifact yeterlidir; yorum
job'ı çalışmaz. `pull_request_target`, cache ve untrusted artifact yürütmesi
kullanılmaz.

Rapor bulguları açıkça statik adaydır. `tests_for` ilişkisinin bulunmaması test
yokluğunun kanıtı değildir; risk skoru merge gate değildir. Bu pilot model
tokenı, kota veya gerçek dünya tasarrufu ölçmez. Silinen veya tamamen
kaldırılmış semboller HEAD grafında görünmeyebilir. Build, graph veya analysis
`status:error` dönerse workflow başarısız olur ve summary/artifact başarılı
rapor gibi gösterilmez.

Eski audit uygulamalarına başlamadan önce [eşlenmiş uygulama paketlerine](audit-packages-20260908.md) bak; tarihsel çözümü güncel kodla karşılaştırmadan yeniden uygulama.

İlk kurulum stack'inde concurrency PR #3, #2 branch'ine alınır; ardından #1 ve birleşik #2 main'e gider. Bu bir kerelik bağımlı kurulumda commit ancestry'yi koruyan merge commit kullanılır; böylece başka PR'ın geçmişini force-push ile yeniden yazmak gerekmez. Normal bağımsız işlerde yukarıdaki squash akışı sürer.
