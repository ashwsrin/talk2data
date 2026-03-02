'use client';

import { useRef, useEffect, useState } from 'react';

interface ArtifactRendererProps {
  html: string;
}

export default function ArtifactRenderer({ html }: ArtifactRendererProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(400);

  useEffect(() => {
    if (iframeRef.current && iframeRef.current.contentDocument) {
      const doc = iframeRef.current.contentDocument;
      doc.open();
      doc.write(html);
      doc.close();

      // Try to adjust height based on content
      const checkHeight = () => {
        if (iframeRef.current?.contentDocument?.body) {
          const body = iframeRef.current.contentDocument.body;
          const newHeight = Math.max(
            body.scrollHeight,
            body.offsetHeight,
            400
          );
          setHeight(Math.min(newHeight, 800)); // Cap at 800px
        }
      };

      // Wait for content to load
      setTimeout(checkHeight, 100);
      if (iframeRef.current.contentWindow) {
        iframeRef.current.contentWindow.addEventListener('load', checkHeight);
      }
    }
  }, [html]);

  return (
    <div className="my-4 border border-gray-300 dark:border-gray-700 rounded-lg overflow-hidden bg-white">
      <div className="px-3 py-2 bg-gray-100 dark:bg-gray-800 border-b border-gray-300 dark:border-gray-700">
        <span className="text-xs font-medium text-gray-600 dark:text-gray-400">
          Rendered HTML Artifact
        </span>
      </div>
      <iframe
        ref={iframeRef}
        sandbox="allow-same-origin allow-scripts"
        className="w-full border-0"
        style={{ height: `${height}px` }}
        title="Rendered HTML Artifact"
      />
    </div>
  );
}
