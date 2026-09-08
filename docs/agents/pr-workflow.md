# CEVDET PR çalışma düzeni

Kod deposu: https://github.com/zPfizer/CEVDET-code (private).
Yerel Vault ve eski Vault Git geçmişi bu depodan ayrıdır.

## Her bağımsız iş için

1. Kod deposunda `git fetch origin` çalıştır. Güncel `origin/main` üzerinden ayrı `codex/<konu>` branch'i ve komşu bir worktree oluştur. Aynı worktree'de eşzamanlı görev çalıştırma.
2. Bir görev birkaç bağımsız değişiklik içeriyorsa her birini ayrı branch/PR'a ayır. Birbirine zorunlu bağımlı değişiklikler aynı PR'da olabilir.
3. Yalnız görev dosyalarını commit et; `git add .` ile ortak çalışmaları toplama. Yeni dosyayı mevcut `.gitignore` izin listesine açıkça ekle.
4. Odaklı testleri çalıştır. Davranış değişikliği varsa mevcut tam sentetik test paketini de çalıştır. CI Windows/Python 3.14 üzerinde aynı paketi çalıştırır.
5. Branch'i push et ve PR aç. Bitmemiş iş için draft kullan; incelemeye hazır olduğunda ready durumuna geçir. Hedef branch `main` olsun.
6. Codex otomatik incelemesini ve CI sonucunu takip et. Yeni commit sonrasında önceki incelemeyi güncel kabul etme; son commit için yeniden inceleme al. Gerekirse PR yorumunda `@codex review` iste; yorum gönderme yetkisi görev kapsamında yoksa kullanıcıdan al.
7. Bulguları çöz ve gerekli testleri tekrar çalıştır. Kullanıcı onayından sonra squash merge yap; otomatik merge açma. GitHub koruması varsa yeşil kontroller ve çözülmüş konuşmalar aranır.
8. Sonraki bağımsız işi güncel `origin/main` üzerinden başlat. Worktree temiz ve kullanım dışı olmadan kaldırma; başka görevin branch'ini değiştirme.

## İlk aktarım

İlk PR, daha önce hazırlanmış kod aktarımı ile PR altyapısını getirir; uzak `main` başlangıçta yalnız README ve ignore kurallarını içerir. İlk PR merge edilmeden bağımsız geliştirmeyi boş uzak `main` üzerinden başlatma. Merge sonrası güncel `origin/main` temel alınır.

## Yerel Vault'a teslim

PR merge edilmesi canlı Vault'u değiştirmez. Açık hedef ve uygulama yetkisiyle, onaylanan runtime diff'i yerel Vault'ta ayrı branch/worktree üzerinden denetlenir. Hedef dosyalardaki mevcut değişiklikler karşılaştırılır; kör klasör kopyası yapılmaz. Vault notları, günlükler, knowledge, özel ayarlar, config ve AGENTS otomatik aktarılmaz. İlgili test/doctor kontrollerinden sonra canlı uygulama ayrıca doğrulanır.

## İnceleme kurulumu

Codex ayarlarında bu depo için otomatik Code review etkinleştirilir. Review kuralları kök `AGENTS.md` içindedir. Bunlar CI veya kullanıcı merge onayının yerine geçmez. GitHub hesap planı branch protection desteklemiyorsa sunucu tarafı zorunluluk varmış gibi raporlama.

Kaynak: https://learn.chatgpt.com/docs/third-party/github (8 Eylül 2026).
