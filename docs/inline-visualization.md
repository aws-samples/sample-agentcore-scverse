# Inline Visualization via Presigned URLs

This document explains how scverse tool outputs (QC plots, UMAP
visualizations) render as inline images in the chat interface.

## How It Works

```
1. User asks: "Run QC on my PBMC3k dataset"

2. Agent calls run_scrna_qc tool via AgentCore Gateway

3. Gateway Lambda routes to the Container Lambda, which:
   - Runs scanpy QC on the researcher's .h5ad
   - Uploads PNG plots to S3
   - Generates presigned URLs (1-hour expiry)
   - Returns response with markdown_image fields:
     {
       "plots": {
         "qc_before_filtering": {
           "markdown_image": "![QC Before Filtering](https://s3.presigned.url...)",
           "description": "Violin plots of total counts, genes, MT%"
         }
       }
     }

4. Agent includes the markdown_image value verbatim in its response:
   "Here are the QC results for your PBMC3k dataset:

    ![QC Before Filtering](https://s3.presigned.url...)

    The dataset contains 2,700 cells. After filtering..."

5. Frontend renders the markdown → image appears inline in chat
```

## Frontend Compatibility

Any frontend that renders standard markdown will display these images:

- **Open WebUI**: Works out of the box. Markdown images render natively.
- **Streamlit**: Use `st.markdown()` — images render automatically.
- **Custom React**: Add a ReactMarkdown component with an `img` renderer:

```tsx
import ReactMarkdown from "react-markdown";

<ReactMarkdown
  components={{
    img: ({ src, alt }) => (
      <div style={{ margin: '12px 0' }}>
        <img
          src={src}
          alt={alt || 'Visualization'}
          style={{ maxWidth: '100%', borderRadius: '8px', border: '1px solid #e9ebed' }}
          loading="lazy"
          onError={(e) => { e.currentTarget.style.display = 'none'; }}
        />
        {alt && (
          <div style={{ fontSize: '12px', color: '#5f6b7a', marginTop: '4px' }}>
            {alt}
          </div>
        )}
      </div>
    ),
  }}
>
  {message.content}
</ReactMarkdown>
```

## Presigned URL Configuration

- URLs expire after 1 hour (configurable via `PRESIGNED_URL_EXPIRY` in the Lambda)
- URLs are generated using `s3:GetObject` — the Lambda role needs this permission
- The S3 bucket uses SSE-S3 encryption and blocks public access
- Presigned URLs work regardless of bucket policy because they carry
  the signer's credentials

## Agent System Prompt

The agent's system prompt includes this instruction:

> When a tool returns a markdown_image field, include it verbatim in
> your response so the image renders inline. Do NOT describe the URL
> as text — paste the markdown_image value directly.

This ensures the agent always passes through the presigned URL in
markdown format rather than describing it or summarizing it.

## Troubleshooting

**Images not rendering?**
- Check that the presigned URL hasn't expired (1-hour default)
- Verify the S3 bucket exists and the Lambda has `s3:GetObject` permission
- Check browser console for CORS errors (shouldn't happen with presigned URLs)
- Ensure your frontend renders markdown — plain text won't work

**Broken image icon?**
- The `onError` handler in the React example hides broken images
- Re-invoke the tool to generate fresh presigned URLs (they expire after 1 hour)
