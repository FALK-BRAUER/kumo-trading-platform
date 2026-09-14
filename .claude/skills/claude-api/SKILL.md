---
name: claude-api
description: Anthropic SDK patterns — model selection, streaming, tool use, multi-turn conversations. Reference when building features that call Claude directly via @anthropic-ai/sdk.
---

## Setup
```typescript
import Anthropic from '@anthropic-ai/sdk';
const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });
```

## Model Selection
| Use Case | Model | Why |
|---|---|---|
| Complex reasoning, long context | `claude-opus-4-6` | Best quality |
| Everyday tasks, coding | `claude-sonnet-4-6` | Speed + quality balance |
| Classification, tagging, cheap tasks | `claude-haiku-4-5-20251001` | Fast + cheap |

## Basic Call
```typescript
const message = await client.messages.create({
  model: 'claude-sonnet-4-6',
  max_tokens: 1024,
  messages: [{ role: 'user', content: 'Hello' }]
});
console.log(message.content[0].text);
```

## Streaming
```typescript
const stream = await client.messages.stream({
  model: 'claude-sonnet-4-6',
  max_tokens: 1024,
  messages: [{ role: 'user', content: prompt }]
});
for await (const chunk of stream) {
  if (chunk.type === 'content_block_delta') process.stdout.write(chunk.delta.text);
}
```

## Tool Use
```typescript
const response = await client.messages.create({
  model: 'claude-sonnet-4-6',
  max_tokens: 1024,
  tools: [{
    name: 'get_weather',
    description: 'Get current weather for a location',
    input_schema: { type: 'object', properties: { location: { type: 'string' } }, required: ['location'] }
  }],
  messages: [{ role: 'user', content: 'What is the weather in Singapore?' }]
});
// Check response.stop_reason === 'tool_use' then handle tool call
```

## Key Rules
- Never hardcode API keys — use environment variables
- Set `max_tokens` explicitly — no default, will error without it
- Handle `overloaded_error` with exponential backoff (happens at peak load)
- Use `system` parameter for persistent instructions, not user messages
- Track token usage: `message.usage.input_tokens + output_tokens` for cost monitoring
