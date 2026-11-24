# 📺 IPTV Avto-Təmir

Playlist-dəki işləməyən kanalları avtomatik tapır və ehtiyat siyahılardan işləyənlə əvəz edir.
GitHub Actions gündə 2 dəfə özü işləyir, dəyişiklik olsa `playlist.m3u`-nu commit edir.
Televizo-da bir dəfə raw linki əlavə edirsən, sonra heç nə etmirsən.

## Necə işləyir

1. `playlist.m3u`-dakı hər kanal yoxlanılır: HLS manifest + son seqment, DASH, xam axın. Yalnız "200 OK" kifayət etmir, axının özü də oxunmalıdır.
2. Kanal **ardıcıl 2 dəfə** (`fail_threshold`) işləməsə ölü sayılır. Bu, bir dəfəlik şəbəkə xətasında yaxşı linki silməmək üçündür.
3. Ölü kanalın adına görə `sources.txt` və `backup/*.m3u`-dakı siyahılarda oxşar adlı kanallar axtarılır. Oxşarlıq həddi 0.9-dur.
   - `HD / FHD / 1080p / (360p)` kimi əlavələr nəzərə alınmır.
   - Rəqəmlər eyni olmalıdır. `Bein Sport 1` heç vaxt `Bein Sport 2` ilə əvəz olunmaz.
   - Kiril və latın uyğunlaşdırılır: `НТВ` = `NTV`.
4. Namizədlər də yoxlanılır, ilk işləyən yazılır. Kanalın adı, loqosu və qrupu dəyişmir, yalnız link (və lazım olsa `#KODIPROP` / `#EXTVLCOPT` sətirləri) dəyişir.
5. Nəticə `reports/status.md`-də, tarixçə `reports/replacements.log`-da olur.

## Quraşdırma (5 dəqiqə)

1. GitHub-da yeni repo yarat, bu qovluğun içindəkiləri yüklə.
2. **Settings → Actions → General → Workflow permissions → Read and write permissions** seç və Save et. Bu olmasa bot commit edə bilməz.
3. Ehtiyat linklərini `sources.txt`-ə yaz (hər sətirdə bir raw `.m3u` linki). GitHub "blob" linki də olar. Faylları `backup/` qovluğuna da atmaq olar.
4. **Actions → "Playlist yoxla və yenilə" → Run workflow** ilə ilk dəfə əl ilə işə sal.
5. Televizo-ya bu linki ver:
   `https://raw.githubusercontent.com/<istifadəçi>/<repo>/main/playlist.m3u`
   (GitHub raw keşi bir neçə dəqiqə gecikə bilər.)

## Ayarlar (`config.json`)

| Açar | Mənası |
|---|---|
| `fail_threshold` | Neçə ardıcıl uğursuzluqdan sonra əvəz olunsun (2 = təxminən 1 gün) |
| `match_threshold` | Ad oxşarlığı, 0.9. Aşağı salsan səhv əvəzləmə riski artır |
| `max_candidates` | Bir kanal üçün ən çox neçə namizəd yoxlansın |
| `protected` | Adında bu sözlər olan kanallara toxunulmur |
| `aliases` | Fərqli adla yazılan kanallar: `"Xezer TV": ["Khazar TV"]` |

## Kompüterdə / telefonda əl ilə işə salmaq

```bash
pip install requests
python scripts/checker.py --now --dry-run   # yalnız göstər, yazma
python scripts/checker.py --now             # dərhal əvəz et
python scripts/checker.py --check-only      # yalnız yoxla
```

## 🌍 AZ / TR kanalları niyə "ölü" görünürdü və necə həll olundu

GitHub Actions ABŞ-dakı serverlərdən işləyir. AZ və TR kanallarının çoxu (ATV, İctimai, Baku TV, Show/Star/Kanal D, TRT və s.) xaricdən açılmır, ona görə köhnə versiya onları "ölü" sayıb səhv linklə əvəz edirdi.

Yeni davranış:
- Yoxlayıcı öz ölkəsini tapır (`ipinfo.io`). **AZ və ya TR deyilsə**, geo linklər (`BUILTIN_GEO` + `config.json → geo_hosts`) **yoxlanmır və dəyişdirilmir**. Hesabatda "Yoxlanmadı" kimi görünür.
- Yoxlananların 60%-dən çoxu ölü çıxsa (`max_dead_ratio`), skript şəbəkə problemi sayıb **heç nə yazmır**.
- Yoxlana bilməyən link əvəz kimi seçilmir.

Geo kanalları da **həqiqətən** yoxlamaq üçün üç yol var:
1. **Evdə işlət:** `run-local.sh` (Linux/Mac/Termux/Pi) və ya `run-local.bat` (Windows). Yoxlayıcı AZ IP-sindən getdiyi üçün nəticə dəqiq olur.
2. **Self-hosted runner:** evdəki kompüteri GitHub-a runner kimi qoş (Settings → Actions → Runners), `update.yml`-də `runs-on: self-hosted` yaz.
3. **AZ/TR proxy:** `config.json`-da `"geo_proxy": "socks5h://user:pass@host:port"` yaz (`pip install requests[socks]` lazımdır). Pulsuz proxylər etibarsızdır.

Qeyd: 100% işləyən pulsuz IPTV siyahısı mövcud deyil. Linklər müvəqqətidir və hər gün ölür, ona görə bu cür avtomatik təmir lazımdır.

## ⚠️ Bilməli olduqların

- **Coğrafi məhdudiyyət.** Yoxlama GitHub-ın ABŞ/Avropa serverlərindən gedir. Yalnız Azərbaycan və ya Türkiyə IP-sində açılan kanallar orada "ölü" görünə bilər, əvəzində isə evdə işləməyən link seçilə bilər. Belə kanalların adını `protected`-ə yaz. Daha dəqiq nəticə üçün skripti evdəki kompüterdə işlət (yuxarıdakı əmrlər).
- **Vaxtı bitən tokenlər.** `?e=1676581573&st=...` kimi linklərin müddəti çoxdan bitib, onlar həmişə ölü çıxacaq. Skript onları ehtiyat siyahıdan əvəz etməyə çalışacaq.
- **Əvəzi tapılmayan kanallar** playlist-də qalır və `reports/status.md`-də siyahılanır.
- **Repo məxfiliyi.** Playlist-də abunə kimi görünən linklər var (`.../iptv/<uzun-kod>/...`). Public repoda bunlar hamıya açıq olur və bloklana bilər. Repo-nu **private** saxlamaq daha təhlükəsizdir (Actions private repoda da işləyir). Private repoda Televizo üçün raw linkə token lazım olur, ona görə seçim edəndə bunu nəzərə al.
