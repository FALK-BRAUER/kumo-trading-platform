---
name: nextjs-patterns
description: Next.js App Router patterns and conventions. Reference when building Next.js features to follow current best practices.
---

## Core Patterns

### File Conventions
- `page.tsx` — route page (server component by default)
- `layout.tsx` — shared layout (wraps children)
- `loading.tsx` — loading UI (Suspense boundary)
- `error.tsx` — error boundary (must be client component)
- `not-found.tsx` — 404 page

### Server vs Client Components
- Default to server components — they don't ship JS to the browser
- Add `"use client"` only when you need: useState, useEffect, event handlers, browser APIs
- Fetch data in server components, pass as props to client components
- Never import server-only code in client components

### Data Fetching
- Use `fetch()` in server components with caching options
- Use Server Actions for mutations (form submissions, data updates)
- Use `revalidatePath()` or `revalidateTag()` after mutations
- Avoid client-side fetching (useEffect + fetch) unless real-time updates needed

### Route Handlers
- `app/api/*/route.ts` for REST-style API endpoints
- Export named functions: GET, POST, PUT, DELETE
- Use NextRequest/NextResponse for type-safe request/response handling
- Validate request body with Zod

### Metadata
- Export `metadata` object or `generateMetadata()` function from page/layout
- Set title, description, openGraph for each page
