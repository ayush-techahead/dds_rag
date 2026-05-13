# Websites Module

Handles website source configuration, crawl frequency, crawl jobs, crawled page metadata, and indexing of fetched page content into Qdrant.

Current implementation crawls the configured URL itself. Recursive site crawling, sitemap support, robots.txt policy, page deduplication, and HTML-to-Markdown conversion can be added behind the existing service interfaces.
