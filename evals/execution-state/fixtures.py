"""Synthetic source conversations. No expected result is sent to the SUT.

The transcript's quoted identifiers make the deterministic grader independent
of paraphrase. These are user task identifiers, not hidden outcome labels.
"""
from __future__ import annotations

CASE_IDS = tuple(f'EVAL-{n:03}' for n in (29, 31, 45, 46, 47, 48, 49, 50, 51, 52, 53, 75, 76, 77))

BASE = [
    ('user', 'Atlas projesinde ATLAS-ESKI bulut arama işine başla; bulut indeksini kur.'),
    ('user', 'ATLAS-ESKI bulut arama işini tamamen iptal ettim. Bu dalın sıradaki adımı artık geçerli değil.'),
    ('user', 'Atlas projesinde yeni ATLAS-AKTIF hedefi için işe başla: çevrimdışı arama. Yerel önbelleği seçtim; gerekçe ağsız çalışma. Açık alt iş ATLAS-TAMAM: veri modelini doğrula. Açık alt iş ATLAS-ACIK: kapasite sınırını ölç. ATLAS-BEKLEYEN alt işi Orion ekibinin ORION-KANIT cevabına bağlı. Sıradaki adım ATLAS-SONRA: kapasite ölçümünü çalıştır.'),
    ('user', 'Yalnız ATLAS-TAMAM veri modeli alt işi doğrulandı ve tamamlandı. Ana ATLAS-AKTIF hedefi ve diğer açık işler devam ediyor.'),
]

SOURCES = {
    'EVAL-029': [('user', '2026-09-03: Atlas için KARAR-A seçtim, gerekçe sadelik.'), ('user', '2026-09-04: Atlas kararımı düzeltiyorum: KARAR-B kullanılacak, gerekçe doğruluk. Önceki karar artık tarihseldir.')],
    'EVAL-031': BASE + [('user', 'Atlas ATLAS-AYAR kararını 32 seçtim, gerekçe ilk ölçüm.'), ('user', 'Orion için YENI-ORION kararım 64; bu ayrı iş. Atlas ATLAS-TAMAM ilerlemesi doğrulandı ve geçerlidir.'), ('user', 'Yalnız Atlas ATLAS-AYAR kararını 48 olarak düzelt; gerekçe yeni kapasite ölçümü. Diğer işler ve doğrulanmış ilerleme aynen kalsın.')],
    'EVAL-045': [('user', 'Atlas için yalnız yerel depolamayı seçtim. Bulut depolamayı reddettim; ağsız çalışmak gerekiyor.')],
    'EVAL-046': BASE,
    'EVAL-047': BASE + [('user', 'Ara soru: Güneş neden sarı görünür?'), ('assistant', 'Atmosfer ışığın algılanan rengini etkiler.'), ('user', 'Atlas işinde nerede kalmıştık?')],
    'EVAL-048': BASE + [('user', 'Oturumu kapatıyorum. ATLAS-ACIK kapasite ölçümü henüz yapılmadı; sonraki oturumda devam edeceğiz.')],
    'EVAL-049': [('user', 'ATLAS-KAPALI rapor işi tamamlandı; doğrulama çıktısını gözden geçirdim, açık işi kalmadı.'), ('user', 'Devam.')],
    'EVAL-050': BASE + [('user', 'Bulut arama konusunu tekrar konuşalım: ATLAS-ESKI neden iptal edilmişti? Bu soru iptal kararını değiştirmiyor.')],
    'EVAL-051': [('assistant', 'Atlas için SECILMEYEN-BULUT veya SECILEN-YEREL kullanılabilir.'), ('user', 'SECILEN-YEREL seçiyorum, gerekçe çevrimdışı kullanım.'), ('user', 'SECILMEYEN-BULUT yalnız konuştuğumuz alternatiftir; onu seçmedim.')],
    'EVAL-052': [('user', 'Atlas için BELKI-SONRA arşiv özelliğini belki sonra yaparız; henüz karar vermedim ve iş taahhüdü yok.')],
    'EVAL-053': BASE + [('user', 'Orion ekibinden ORION-KANIT cevabı geldi: kapasite 48. Bu cevap Atlas ATLAS-BEKLEYEN işi içindir; diğer açık ATLAS-ACIK işi devam ediyor.')],
    'EVAL-075': BASE,
    'EVAL-076': BASE,
    'EVAL-077': BASE,
}

QUERIES = {case_id: 'Atlas işinde nerede kalmıştık?' for case_id in CASE_IDS}
QUERIES.update({'EVAL-029': 'Şu an Atlas için ne kullanıyoruz?', 'EVAL-031': 'Atlas ve Orion kararları ile doğrulanmış ilerleme nedir?', 'EVAL-045': 'Atlas depolama kararım nedir?', 'EVAL-049': 'Devam.', 'EVAL-052': 'BELKI-SONRA kesinleşmiş bir iş mi?'})


def agent_input(case_id: str) -> dict:
    """Return only production-equivalent role/text source plus user query."""
    if case_id not in CASE_IDS:
        raise ValueError('unknown-case')
    messages = list(SOURCES[case_id])
    if case_id == 'EVAL-049':
        messages.insert(0, ('user', 'Atlas projesinde ATLAS-KAPALI rapor işine başla.'))
    elif case_id in {'EVAL-029', 'EVAL-045', 'EVAL-051', 'EVAL-052'}:
        messages.insert(0, ('user', 'Atlas projesinde depolama kararını belirleme işine başla.'))
    return {'messages': [{'role': role, 'text': text} for role, text in messages], 'query': QUERIES[case_id]}
