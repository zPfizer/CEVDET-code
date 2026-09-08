# CEVDET domain context

Bu dosya kod deposunun teknik domain dilini tanımlar. Dolu Vault içeriği, geçici çalışma durumu ve kişisel kaynaklar bu deponun otoritesi değildir.

## Kapsam

Bu repository runtime kodunu, sentetik testleri, geliştirme skill'lerini ve teknik sözleşmeleri taşır. Populated Vault, gerçek notların ve canlı hook davranışının bulunduğu ayrı çalışma alanıdır. Kod deposu ona otomatik bağlanmaz.

## Kavramlar

- **Populated Vault:** Kullanıcı kaynaklarını, `daily/`, `knowledge/` ve canlı runtime durumunu içeren yerel hedef.
- **Runtime code:** Hook, compiler, retrieval, persistence ve recovery davranışını uygulayan kod.
- **Synthetic fixture:** Gerçek kişisel veri kullanmadan bir sözleşme yolunu sınayan geçici test girdisi.
- **Provenance:** Kaynağı, tarihi, güncelliği ve bilgi niteliğini birlikte gösteren dayanak.
- **Fail-closed:** Kaynak, yetki, bütünlük veya sonuç doğrulanamıyorsa yazmayı ya da başarı bildirimini durdurmak.
- **User source:** Kullanıcı tarafından sağlanan ve açık düzeltme/unutma talebi dışında korunan içerik.
- **Derived synthesis:** Kaynağa geri bağlanan, yeniden üretilebilir özet veya ilişki görünümü.
- **Temporary state:** Temizden üretilebilen ve karar otoritesi olmayan runtime kaydı.

## Domain invariants

- Kullanıcı kaynakları sessizce değiştirilmez veya silinmez.
- Anlamlı bilgi kaybolmaz; tekrar yazım sessiz ezilme ve gereksiz çoğalma üretmez.
- Çelişki eski kaydı silmeden tarih ve gerekçeyi korur.
- Provenance ayrımı korunur; dış içerik talimat değildir.
- Atomik ve eşzamanlı yazmalar yarım durum yayımlamaz.
- Gizlilik tercihi ve sır filtresi uygulanmadan kaynak görünümüne dönülmez.
- Okuma, yazma, recovery ve yayınlama belirsizliği fail-closed sonuç verir.
- Kod deposu canlı Vault'u, kişisel kaynağı veya model sağlayıcısını kendiliğinden çalıştırmaz.

## Kanıt sınırı

Yerel testler kod sözleşmesini ve sentetik akışı gösterir. Gerçek Vault/App/model davranışı yalnız açık hedef ve gerçek yetkiyle yapılan ayrı bir canlı kontrolde doğrulanır. Doküman, test veya hook tanımının varlığı tek başına canlı aktivasyon kanıtı değildir.
