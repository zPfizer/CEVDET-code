---
name: beyin-doktor
description: Kullanıcı açıkça "beyin doktor" dediğinde bu code checkout'undaki test ve runtime sözleşmesini salt okunur tanıla; canlı Vault onarımı için hedef ve gerçek yetki ayrıca gerekir.
---

# Beyin Doktor

Bu skill code repository içinde yerel tanı içindir. Dolu Vault kaynaklarını bu checkout'a kopyalamaz ve canlı hook veya model çalıştırmaz.

1. Aktif code checkout kökünü belirle ve tanı boyunca aynı kökü kullan.
2. Code-only checkout'ta populated Vault doctor sonucunu varsayma. Önce ilgili sentetik testleri ve statik config/hook kontrollerini çalıştır.
3. Doctor kontrolü istenirse doctor scriptinin kendi `.codex/scripts/` kopyasının bulunduğu populated Vault'ta çalıştır. `--project-root` sahiplik bağlamıdır; başka bir Vault seçmek için kullanılamaz.
4. Komut tablosunu ve her `FAIL` satırının gerçek kanıtını raporla. Code-only checkout'ta kişisel Vault yüzeylerinin bulunmaması beklenen bir sınır olabilir; bunu Vault dosyası kopyalayarak düzeltme.
5. Tanı isteğinde dosya veya ayar değiştirme. Onarım ayrıca istendiyse yalnız bu checkout'taki task-owned kod/test/doküman yollarında, geri alınabilir kapsamda çalış.
6. Populated Vault tanısı veya onarımı için hedef yolu kullanıcı açıkça vermeli ve gerçek yetki bulunmalıdır. Başka bir Vault'a geçme, hedef kaynakları buraya alma veya çalışan işi üzerine yazma.
7. Tekrar kontrolü aynı kökte çalıştır; gözlenmeyen canlı hook, App veya model davranışını başarılı sayma.
