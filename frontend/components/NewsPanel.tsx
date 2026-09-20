"use client";

// Recent-news panel. Renders the live FMP news feed from enrichment.news.
// Follows the structure / density / color usage of ValuationSummary.

import React from "react";
import type { Enrichment, NewsItem } from "@/lib/types";
import { fmtDate } from "@/lib/format";
import { Card, EmptyState, Spinner } from "@/components/ui";

function snippet(text: string | null | undefined, max = 220): string {
  if (!text) return "";
  const t = text.trim();
  if (t.length <= max) return t;
  return t.slice(0, max).trimEnd() + "…";
}

function NewsRow({ item }: { item: NewsItem }) {
  const title = item?.title || "Untitled";
  const meta = [item?.site, fmtDate(item?.publishedDate)]
    .filter((p) => p && String(p).length > 0)
    .join(" · ");
  const body = snippet(item?.text);

  const titleEl = item?.url ? (
    <a
      href={item.url}
      target="_blank"
      rel="noopener noreferrer"
      className="font-medium text-ink hover:text-brand"
    >
      {title}
    </a>
  ) : (
    <span className="font-medium text-ink">{title}</span>
  );

  return (
    <div className="flex gap-3">
      {item?.image && (
        <img
          src={item.image}
          alt=""
          width={80}
          height={60}
          loading="lazy"
          className="h-[60px] w-[80px] shrink-0 rounded border border-line object-cover"
          onError={(e) => {
            (e.target as HTMLImageElement).style.display = "none";
          }}
        />
      )}
      <div className="min-w-0 flex-1">
        <div className="text-sm leading-snug">{titleEl}</div>
        {meta && <div className="mt-0.5 text-xs text-ink-faint">{meta}</div>}
        {body && <p className="mt-1 text-sm text-ink-dim">{body}</p>}
      </div>
    </div>
  );
}

export default function NewsPanel({
  enrichment,
  loading,
}: {
  enrichment: Enrichment | null;
  loading: boolean;
}) {
  const news = enrichment?.news ?? [];

  let body: React.ReactNode;
  if (loading) {
    body = (
      <div className="flex items-center justify-center gap-2 py-8 text-sm text-ink-dim">
        <Spinner />
        <span>Loading news…</span>
      </div>
    );
  } else if (!enrichment?.enabled) {
    body = (
      <EmptyState
        title="News feed off"
        hint="Set FMP_API_KEY to enable the live news feed."
      />
    );
  } else if (!news.length) {
    body = <EmptyState title="No recent news" />;
  } else {
    body = (
      <div className="space-y-3">
        {news.map((item, i) => (
          <NewsRow key={item?.url || `${item?.title || "item"}-${i}`} item={item} />
        ))}
      </div>
    );
  }

  return <Card title="Recent news">{body}</Card>;
}
