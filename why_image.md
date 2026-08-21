# Why Image-to-Video is Better for Complex Scenes

## The Problem with Text-to-Video Only

Veo 3.1 is a generative model — it interprets prompts creatively, not literally.

**What doesn't work reliably with text-only:**
- Specific props in hands (character holding a cookie)
- Two characters with exact positioning
- Fantasy/abstract character designs (food-shaped people)
- Multiple simultaneous details in one prompt
- Consistent character appearance across parts

**What works well:**
- Simple scenes with clear subjects
- One main action
- Realistic environments
- Motion from a reference image

## The Solution: Image → Video Pipeline

1. Generate a PRECISE reference image using Imagen 4 (text-to-image is much more controllable)
2. Feed that image to Veo as the first frame
3. Veo animates the image — characters look EXACTLY like the image
4. Text prompt only describes MOTION (not appearance — image handles that)

## How It Works in This Pipeline

When `--image` mode is enabled:

1. LLM generates script + image prompts for key frames
2. Imagen 4 generates reference images (stored in assets folder)
3. Part 1: reference_image + motion_prompt → Veo (image-to-video)
4. Part 2+: extends from Part 1 video (native extend)

## Key Rules for Image Prompts

- Image prompt describes the STATIC SCENE (characters, environment, lighting, composition)
- Video prompt describes only the MOTION (what moves, what changes)
- Image prompt can be very detailed (Imagen handles complex scenes better than Veo)
- Keep video/motion prompt simple (Veo just needs to animate what's already there)

## Imagen 4 API

- Model: `imagen-4.0-generate-001`
- Endpoint: Same Vertex AI predict endpoint
- Supports: 9:16 portrait aspect ratio
- Returns: base64 encoded PNG image

## Veo Image-to-Video Rules (from Google docs)

- Source image provides subject, scene, and style
- Prompt should focus on MOTION only (don't re-describe the image)
- Use general terms: "the character", "the figure", "they"
- Direct camera movement, subject animation, environmental changes
- High-quality source image = better video output
