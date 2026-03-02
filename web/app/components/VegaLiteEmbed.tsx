'use client';

import { useEffect, useRef, useState } from 'react';

interface VegaLiteEmbedProps {
  spec: string | Record<string, unknown>;
}

export default function VegaLiteEmbed({ spec }: VegaLiteEmbedProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    setError(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = typeof spec === 'string' ? (JSON.parse(spec) as Record<string, unknown>) : spec;
    } catch (e) {
      setError('Invalid Vega-Lite JSON');
      return;
    }
    if (!parsed || typeof parsed !== 'object') {
      setError('Invalid spec');
      return;
    }
    // Patch spec with default width/height when missing so chart is not shrunk
    if (!('width' in parsed) || parsed.width == null) {
      parsed.width = 700;
    }
    if (!('height' in parsed) || parsed.height == null) {
      parsed.height = 450;
    }
    const container = containerRef.current;
    const run = async () => {
      if (!container) return;
      try {
        container.innerHTML = '';
        const vegaEmbed = (await import('vega-embed')).default;
        await vegaEmbed(container, parsed as object, {
          actions: true,
          renderer: 'canvas',
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to render chart');
      }
    };
    run();
    return () => {
      if (container) container.innerHTML = '';
    };
  }, [spec]);

  if (error) {
    return (
      <div className="my-3 rounded border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 px-3 py-2 text-sm text-amber-800 dark:text-amber-200">
        Vega-Lite: {error}
      </div>
    );
  }
  return <div ref={containerRef} className="vega-lite-embed my-3 w-full min-h-[420px]" />;
}
