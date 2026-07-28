<?xml version="1.0" encoding="UTF-8"?>
<!-- Витрина RSS-канала для браузера. Читалки инструкцию обработки
     <?xml-stylesheet?> игнорируют и видят тот же RSS, что видели всегда, —
     ломать 178 подписок FreshRSS этот файл не может по построению.
     XSLT только 1.0: другого браузеры не умеют. -->
<xsl:stylesheet version="1.0"
                xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
                xmlns="http://www.w3.org/1999/xhtml">
  <xsl:output method="html" encoding="UTF-8" indent="yes"
              doctype-system="about:legacy-compat"/>

  <!-- «Mon, 27 Jul 2026 19:45:12 +0000» -> «27 июля 2026, 19:45». Разбор по
       позициям: в RSS формат даты фиксирован спецификацией. -->
  <xsl:template name="date">
    <xsl:param name="rfc"/>
    <xsl:value-of select="number(substring($rfc, 6, 2))"/>
    <xsl:text> </xsl:text>
    <xsl:variable name="m" select="substring($rfc, 9, 3)"/>
    <xsl:choose>
      <xsl:when test="$m = 'Jan'">января</xsl:when>
      <xsl:when test="$m = 'Feb'">февраля</xsl:when>
      <xsl:when test="$m = 'Mar'">марта</xsl:when>
      <xsl:when test="$m = 'Apr'">апреля</xsl:when>
      <xsl:when test="$m = 'May'">мая</xsl:when>
      <xsl:when test="$m = 'Jun'">июня</xsl:when>
      <xsl:when test="$m = 'Jul'">июля</xsl:when>
      <xsl:when test="$m = 'Aug'">августа</xsl:when>
      <xsl:when test="$m = 'Sep'">сентября</xsl:when>
      <xsl:when test="$m = 'Oct'">октября</xsl:when>
      <xsl:when test="$m = 'Nov'">ноября</xsl:when>
      <xsl:otherwise>декабря</xsl:otherwise>
    </xsl:choose>
    <xsl:text> </xsl:text>
    <xsl:value-of select="substring($rfc, 13, 4)"/>
    <xsl:text>, </xsl:text>
    <xsl:value-of select="substring($rfc, 18, 5)"/>
  </xsl:template>

  <!-- Домен из ссылки: «https://www.livemint.com/news/…» -> «livemint.com».
       В XSLT 1.0 нет regex, поэтому режем по разделителям. -->
  <xsl:template name="domain">
    <xsl:param name="url"/>
    <xsl:variable name="host" select="substring-before(concat(substring-after($url, '//'), '/'), '/')"/>
    <xsl:choose>
      <xsl:when test="starts-with($host, 'www.')"><xsl:value-of select="substring($host, 5)"/></xsl:when>
      <xsl:otherwise><xsl:value-of select="$host"/></xsl:otherwise>
    </xsl:choose>
  </xsl:template>

  <!-- В description лежит текст статьи целиком: двадцать таких подряд — это
       не лента, а стена. Режем до тех же 260 символов, что и на сайте
       (site._snippet), по границе слова — заодно отсекается мусор вроде
       «<br><br>» и биографии автора, который живёт ниже по тексту. -->
  <xsl:template name="snippet">
    <xsl:param name="text"/>
    <xsl:choose>
      <xsl:when test="string-length($text) &lt;= 260">
        <xsl:value-of select="$text"/>
      </xsl:when>
      <xsl:otherwise>
        <xsl:variable name="cut">
          <xsl:call-template name="cut-word">
            <xsl:with-param name="s" select="substring($text, 1, 260)"/>
          </xsl:call-template>
        </xsl:variable>
        <!-- «Мантар....» — точка перед многоточием читается как опечатка. -->
        <xsl:choose>
          <xsl:when test="contains('.,;:—–-', substring($cut, string-length($cut)))">
            <xsl:value-of select="substring($cut, 1, string-length($cut) - 1)"/>
          </xsl:when>
          <xsl:otherwise><xsl:value-of select="$cut"/></xsl:otherwise>
        </xsl:choose>
        <xsl:text>…</xsl:text>
      </xsl:otherwise>
    </xsl:choose>
  </xsl:template>

  <!-- Отбросить хвост последнего слова. В XSLT 1.0 «найти последний пробел»
       делается только рекурсией. -->
  <xsl:template name="cut-word">
    <xsl:param name="s"/>
    <xsl:choose>
      <xsl:when test="string-length($s) = 0"/>
      <xsl:when test="substring($s, string-length($s)) = ' '">
        <xsl:value-of select="substring($s, 1, string-length($s) - 1)"/>
      </xsl:when>
      <xsl:otherwise>
        <xsl:call-template name="cut-word">
          <xsl:with-param name="s" select="substring($s, 1, string-length($s) - 1)"/>
        </xsl:call-template>
      </xsl:otherwise>
    </xsl:choose>
  </xsl:template>

  <xsl:template match="/rss/channel">
    <html lang="ru" data-theme="auto">
      <head>
        <meta charset="UTF-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <meta name="color-scheme" content="light dark"/>
        <title><xsl:value-of select="title"/> — RSS</title>
        <link rel="stylesheet" href="/static/fonts.css"/>
        <link rel="stylesheet" href="/static/style.css"/>
        <link rel="icon" href="/static/mark.svg" type="image/svg+xml"/>
        <!-- Тот же выбор темы, что на сайте: иначе переход по ссылке на фид
             сбрасывает тёмную обратно в системную. -->
        <script>
          try {
            var t = localStorage.getItem('theme');
            if (t) document.documentElement.dataset.theme = t;
          } catch (e) {}
        </script>
      </head>
      <body>
        <header class="top">
          <div class="top__bar wrap">
            <a class="mark" href="/">
              <span class="mark__glyph" aria-hidden="true"></span>
              <span class="mark__word">Bhutyan<span class="mark__tld">.online</span></span>
            </a>
          </div>
        </header>

        <main class="page page--prose wrap">
          <nav class="crumbs" aria-label="Хлебные крошки">
            <a href="/">Лента</a> <span aria-hidden="true">/</span> <span>RSS-канал</span>
          </nav>

          <header class="page__head">
            <h1 class="page__title"><xsl:value-of select="title"/></h1>
            <p class="page__note"><xsl:value-of select="description"/></p>
          </header>

          <!-- Главное на странице: человек попал сюда по ссылке и видит
               непонятное. Первым делом — что это и что с этим делать. -->
          <div class="hint">
            <p class="hint__lead">Это не страница, а <b>RSS-канал</b> — он обновляется сам,
              и его читают программы-читалки.</p>
            <p class="hint__body">Скопируйте адрес из строки браузера и вставьте его в свою
              читалку. Если читалки нет — можно взять готовую, со всеми подписками уже
              внутри: <a href="https://freshrss.bhutyan.online">freshrss.bhutyan.online</a>.
              Как всё это устроено, написано <a href="/about.html">здесь</a>.</p>
          </div>

          <p class="feed__stamp">
            <xsl:value-of select="count(item)"/><xsl:text> </xsl:text>
            <xsl:choose>
              <xsl:when test="count(item) = 1">запись</xsl:when>
              <xsl:when test="count(item) &lt; 5">записи</xsl:when>
              <xsl:otherwise>записей</xsl:otherwise>
            </xsl:choose>
            <xsl:if test="lastBuildDate">
              <xsl:text> · обновлено </xsl:text>
              <xsl:call-template name="date">
                <xsl:with-param name="rfc" select="lastBuildDate"/>
              </xsl:call-template>
            </xsl:if>
          </p>

          <div class="feed">
            <xsl:for-each select="item">
              <article class="story">
                <h2 class="story__title">
                  <a class="story__link" rel="noopener nofollow">
                    <xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute>
                    <xsl:value-of select="title"/>
                  </a>
                </h2>
                <p class="story__meta">
                  <span class="story__source">
                    <xsl:call-template name="domain">
                      <xsl:with-param name="url" select="link"/>
                    </xsl:call-template>
                  </span>
                  <time class="story__time">
                    <xsl:call-template name="date">
                      <xsl:with-param name="rfc" select="pubDate"/>
                    </xsl:call-template>
                  </time>
                </p>
                <p class="story__snippet">
                  <xsl:call-template name="snippet">
                    <xsl:with-param name="text" select="normalize-space(description)"/>
                  </xsl:call-template>
                </p>
              </article>
            </xsl:for-each>
          </div>

          <xsl:if test="count(item) = 0">
            <p class="empty">За выбранное окно ничего не набралось — канал пустой,
              но живой: следующая сборка положит сюда новости, как только они появятся.</p>
          </xsl:if>
        </main>

        <footer class="foot">
          <div class="wrap foot__row">
            Bhutyan.online — автоматическая витрина. Источник:
            <a href="https://www.gdeltproject.org/">GDELT GKG</a>.
          </div>
        </footer>
      </body>
    </html>
  </xsl:template>
</xsl:stylesheet>
