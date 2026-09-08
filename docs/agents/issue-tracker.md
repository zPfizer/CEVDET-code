# Issue and verification notes

Bu repository'de uygulanabilir değişikliklerin ana kaydı branch, diff, test sonucu ve PR'dır. Kişisel Vault kararları ve runtime günlükleri bu depoya taşınmaz.

## Yerel çalışma alanı

- `.scratch/`, geçici kopyalar, test çıktıları ve çalışma state'i yerel geliştirme alanıdır; Git'e eklenmez ve Vault notu değildir.
- Gerçek kullanıcı verisini veya populated Vault'un `daily/`, `knowledge/`, özel ayar ve state dosyalarını issue kanıtı olarak kopyalama.
- Sentetik fixture, hata çıktısı ve kabul notu değişikliğin kapsamını ve yeniden üretme komutunu gösterecek kadar tutulur.
- Kalıcı teknik karar gerekiyorsa ilgili `docs/` belgesini ve PR açıklamasını güncelle; ikinci bir görev kaydı oluşturma.

## Değişiklik kaydı

Her task-owned round için:

1. Kapsamı ve etkilenen dosyaları belirle.
2. İlgili kod, çağrılar ve test kanıtını incele.
3. En küçük diff'i uygula.
4. Odaklı testleri ve gerekli geniş kapıları çalıştır.
5. Diff, çalışma ağacı ve test sonucunu PR'da raporla.

Başarısız veya sıfır test sonucu başarı değildir. Canlı Vault/App/model kontrolü yapılmadıysa bunu yerel test sonucundan ayrı tut.

## Teslim ve uygulama

Branch -> yerel testler -> push -> PR -> kullanıcı onayı -> merge sırasını koru. Merge sonrasında yalnız onaylanmış runtime kodunu açık hedef Vault'a uygula. Notları, config'i, `AGENTS.md`'yi veya çalışan Vault işini otomatik kopyalama ya da üzerine yazma.
