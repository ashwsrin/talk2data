import { NextRequest } from 'next/server';

export const dynamic = 'force-dynamic';

async function handleProxy(req: NextRequest, { params }: { params: Promise<{ path?: string[] }> }) {
    const backendUrl = process.env.API_URL || 'http://127.0.0.1:8001';

    // Resolve params (in Next.js 15, params is a Promise)
    const resolvedParams = await params;

    // Construct the target URL
    const pathPrefix = resolvedParams.path ? '/' + resolvedParams.path.join('/') : '';
    const searchParams = req.nextUrl.searchParams.toString();
    const queryString = searchParams ? `?${searchParams}` : '';
    const targetUrl = `${backendUrl}/api${pathPrefix}${queryString}`;

    try {
        const headers = new Headers();
        if (req.headers.has('content-type')) headers.set('Content-Type', req.headers.get('content-type') as string);
        if (req.headers.has('Authorization')) headers.set('Authorization', req.headers.get('Authorization') as string);

        const fetchOptions: RequestInit = {
            method: req.method,
            headers,
        };

        if (req.method !== 'GET' && req.method !== 'HEAD') {
            fetchOptions.body = await req.text(); // Read body as raw text to pass through exactly
        }

        const res = await fetch(targetUrl, fetchOptions);

        // Return the readable stream from the backend natively
        return new Response(res.body, {
            status: res.status,
            headers: {
                'Content-Type': res.headers.get('content-type') || 'text/plain',
                'Cache-Control': 'no-cache, no-transform',
                'X-Content-Type-Options': 'nosniff',
            },
        });
    } catch (error) {
        console.error(`Error proxying ${req.method} to backend:`, error);
        return new Response(`Proxy Error: ${error instanceof Error ? error.message : String(error)}`, { status: 500 });
    }
}

export {
    handleProxy as GET,
    handleProxy as POST,
    handleProxy as PUT,
    handleProxy as DELETE,
    handleProxy as PATCH,
};
