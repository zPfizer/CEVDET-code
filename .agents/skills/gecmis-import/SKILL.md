---
name: gecmis-import
description: Eski sohbet arşivi için yalnız açık kullanıcı isteğiyle code repository dışında yerel önizleme veya yetkili hedef Vault aktarımını yönlendirir.
disable-model-invocation: true
---

# Geçmiş İçe Aktarımı

Bu skill code repository'ye arşiv yazmaz. Varsayılan işlem yerel önizlemedir; gerçek aktarım için hedef populated Vault, kapsam ve gerçek yetki açıkça belirtilmelidir.

## Önizleme

1. Kullanıcının verdiği arşiv yolunu ve sağlayıcıyı doğrula; yol veya kişisel arşiv tahmin etme.
2. [Sağlayıcı referansları](references/providers.md) içindeki uygun çözümleyiciyi kullanarak yalnız gerekli yerel veriyi oku.
3. `--preview` akışında dosya yazma, `daily/` veya `knowledge/` üretme ve model çağrısı yapma. Sentetik test verisi dışında gerçek kişisel arşivi bu checkout'a kopyalama.
4. Dahil edilen, atlanan, tarih sınırı ve filtre nedenlerini kaynak çıktısından raporla. Önizleme yazma veya model işleme yetkisi vermez.

## Açık hedef Vault işlemi

Kullanıcı gerçek aktarım isterse hedef populated Vault yolunu, yazma kapsamını ve gerçek yetkiyi açıkça doğrula. Aktarımı hedefteki dağıtılmış runtime ile yürüt; code repository çalışma ağacını değiştirme.

- Kullanım dışı veya hassas sohbet filtrelerini aktarım öncesinde uygula.
- Mevcut hedef dosyaları silme, birleştirme veya üzerine yazma.
- Yerel dosya yazımı model işlemenin tamamlandığını göstermez; her aşamanın sonucunu ayrı doğrula.
- Model sağlayıcısı, hesap, ücret, zamanlama veya başarı hakkında kaynakta olmayan söz verme.
- Kullanıcı yalnız önizleme istediyse önizlemede bitir; otomatik ingestion, akşam derlemesi veya sonradan silmeyle vazgeçme varsayma.

Bu skill'i kullanıcı açıkça çağırmadıkça çalıştırma. Gerçek hedef Vault işlemi bu repository'nin test ve dokümantasyon sınırının dışındadır.
