/* -*- Mode: C++; tab-width: 2; indent-tabs-mode: nil; c-basic-offset: 2 -*-
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

#include "mozilla/layers/PLayersParent.h"
#include "BasicCanvasLayer.h"
#include "gfxImageSurface.h"
#include "GLContext.h"
#include "gfxUtils.h"
#include "gfxPlatform.h"
#include "mozilla/Preferences.h"
#include "CanvasClient.h"
#include "BasicLayersImpl.h"

#include "nsXULAppAPI.h"

#include "gfxPlatform.h"

using namespace mozilla::gfx;

namespace mozilla {
namespace layers {

void
BasicCanvasLayer::Initialize(const Data& aData)
{
  NS_ASSERTION(mSurface == nullptr, "BasicCanvasLayer::Initialize called twice!");

  if (aData.mSurface) {
    mSurface = aData.mSurface;
    NS_ASSERTION(aData.mGLContext == nullptr,
                 "CanvasLayer can't have both surface and GLContext");
    mNeedsYFlip = false;
  } else if (aData.mGLContext) {
    NS_ASSERTION(aData.mGLContext->IsOffscreen(), "canvas gl context isn't offscreen");
    mGLContext = aData.mGLContext;
    mGLBufferIsPremultiplied = aData.mGLBufferIsPremultiplied;
    mCanvasFramebuffer = mGLContext->GetOffscreenFBO();
    mNeedsYFlip = true;
  } else if (aData.mDrawTarget) {
    mDrawTarget = aData.mDrawTarget;
    mSurface = gfxPlatform::GetPlatform()->GetThebesSurfaceForDrawTarget(mDrawTarget);
    mNeedsYFlip = false;
  } else {
    NS_ERROR("CanvasLayer created without mSurface, mDrawTarget or mGLContext?");
  }

  mBounds.SetRect(0, 0, aData.mSize.width, aData.mSize.height);
}

void
BasicCanvasLayer::UpdateSurface(gfxASurface* aDestSurface, Layer* aMaskLayer)
{
  if (!IsDirty())
    return;
  Painted();

  if (mDrawTarget) {
    mDrawTarget->Flush();
    // TODO Fix me before turning accelerated quartz canvas by default
    //mSurface = gfxPlatform::GetPlatform()->GetThebesSurfaceForDrawTarget(mDrawTarget);
  }

  if (!mGLContext && aDestSurface) {
    nsRefPtr<gfxContext> tmpCtx = new gfxContext(aDestSurface);
    tmpCtx->SetOperator(gfxContext::OPERATOR_SOURCE);
    BasicCanvasLayer::PaintWithOpacity(tmpCtx, 1.0f, aMaskLayer);
    return;
  }

  if (mGLContext) {
    if (aDestSurface && aDestSurface->GetType() != gfxASurface::SurfaceTypeImage) {
      NS_ASSERTION(aDestSurface->GetType() == gfxASurface::SurfaceTypeImage,
                   "Destination surface must be ImageSurface type");
      return;
    }

    // We need to read from the GLContext
    mGLContext->MakeCurrent();

#if defined (MOZ_X11) && defined (MOZ_EGL_XRENDER_COMPOSITE)
    if (!mForceReadback) {
      mGLContext->GuaranteeResolve();
      gfxASurface* offscreenSurface = mGLContext->GetOffscreenPixmapSurface();

      // XRender can only blend premuliplied alpha, so only allow xrender
      // path if we have premultiplied alpha or opaque content.
      if (offscreenSurface && (mGLBufferIsPremultiplied || (GetContentFlags() & CONTENT_OPAQUE))) {  
        mSurface = offscreenSurface;
        mNeedsYFlip = false;
        return;
      }
    }
#endif

    gfxIntSize readSize(mBounds.width, mBounds.height);
    gfxImageFormat format = (GetContentFlags() & CONTENT_OPAQUE)
                              ? gfxASurface::ImageFormatRGB24
                              : gfxASurface::ImageFormatARGB32;

    nsRefPtr<gfxImageSurface> readSurf;
    nsRefPtr<gfxImageSurface> resultSurf;

    bool usingTempSurface = false;

    if (aDestSurface) {
      resultSurf = static_cast<gfxImageSurface*>(aDestSurface);

      if (resultSurf->GetSize() != readSize ||
          resultSurf->Stride() != resultSurf->Width() * 4)
      {
        readSurf = GetTempSurface(readSize, format);
        usingTempSurface = true;
      }
    } else {
      resultSurf = GetTempSurface(readSize, format);
      usingTempSurface = true;
    }

    if (!usingTempSurface)
      DiscardTempSurface();

    if (!readSurf)
      readSurf = resultSurf;

    if (!resultSurf || resultSurf->CairoStatus() != 0)
      return;

    MOZ_ASSERT(readSurf);
    MOZ_ASSERT(readSurf->Stride() == mBounds.width * 4, "gfxImageSurface stride isn't what we expect!");

    // We need to Flush() the surface before modifying it outside of cairo.
    readSurf->Flush();
    mGLContext->ReadScreenIntoImageSurface(readSurf);
    readSurf->MarkDirty();

    // If the underlying GLContext doesn't have a framebuffer into which
    // premultiplied values were written, we have to do this ourselves here.
    // Note that this is a WebGL attribute; GL itself has no knowledge of
    // premultiplied or unpremultiplied alpha.
    if (!mGLBufferIsPremultiplied)
      gfxUtils::PremultiplyImageSurface(readSurf);

    if (readSurf != resultSurf) {
      MOZ_ASSERT(resultSurf->Width() >= readSurf->Width());
      MOZ_ASSERT(resultSurf->Height() >= readSurf->Height());

      resultSurf->Flush();
      resultSurf->CopyFrom(readSurf);
      resultSurf->MarkDirty();
    }

    // stick our surface into mSurface, so that the Paint() path is the same
    if (!aDestSurface) {
      mSurface = resultSurf;
    }
  }
}

void
BasicCanvasLayer::Paint(gfxContext* aContext, Layer* aMaskLayer)
{
  if (IsHidden())
    return;
  UpdateSurface();
  FireDidTransactionCallback();
  PaintWithOpacity(aContext, GetEffectiveOpacity(), aMaskLayer);
}

void
BasicCanvasLayer::PaintWithOpacity(gfxContext* aContext,
                                   float aOpacity,
                                   Layer* aMaskLayer)
{
  NS_ASSERTION(BasicManager()->InDrawing(),
               "Can only draw in drawing phase");

  if (!mSurface) {
    NS_WARNING("No valid surface to draw!");
    return;
  }

  nsRefPtr<gfxPattern> pat = new gfxPattern(mSurface);

  pat->SetFilter(mFilter);
  pat->SetExtend(gfxPattern::EXTEND_PAD);

  gfxMatrix m;
  if (mNeedsYFlip) {
    m = aContext->CurrentMatrix();
    aContext->Translate(gfxPoint(0.0, mBounds.height));
    aContext->Scale(1.0, -1.0);
  }

  // If content opaque, then save off current operator and set to source.
  // This ensures that alpha is not applied even if the source surface
  // has an alpha channel
  gfxContext::GraphicsOperator savedOp;
  if (GetContentFlags() & CONTENT_OPAQUE) {
    savedOp = aContext->CurrentOperator();
    aContext->SetOperator(gfxContext::OPERATOR_SOURCE);
  }

  AutoSetOperator setOperator(aContext, GetOperator());
  aContext->NewPath();
  // No need to snap here; our transform is already set up to snap our rect
  aContext->Rectangle(gfxRect(0, 0, mBounds.width, mBounds.height));
  aContext->SetPattern(pat);

  FillWithMask(aContext, aOpacity, aMaskLayer);

#if defined (MOZ_X11) && defined (MOZ_EGL_XRENDER_COMPOSITE)
  if (mGLContext && !mForceReadback) {
    // Wait for X to complete all operations before continuing
    // Otherwise gl context could get cleared before X is done.
    mGLContext->WaitNative();
  }
#endif

  // Restore surface operator
  if (GetContentFlags() & CONTENT_OPAQUE) {
    aContext->SetOperator(savedOp);
  }  

  if (mNeedsYFlip) {
    aContext->SetMatrix(m);
  }
}

void
BasicShadowableCanvasLayer::Initialize(const Data& aData)
{
  BasicCanvasLayer::Initialize(aData);
  if (!HasShadow())
      return;

  mCanvasClient = nullptr;
}

void
BasicShadowableCanvasLayer::Paint(gfxContext* aContext, Layer* aMaskLayer)
{
  if (!HasShadow()) {
    BasicCanvasLayer::Paint(aContext, aMaskLayer);
    return;
  }

  if (!IsDirty()) {
    return;
  }

  if (aMaskLayer) {
    static_cast<BasicImplData*>(aMaskLayer->ImplData())
      ->Paint(aContext, nullptr);
  }
  
  if (!mCanvasClient) {
    TextureFlags flags = NoFlags;
    if (mNeedsYFlip) {
      flags |= NeedsYFlip;
    }
    mCanvasClient = BasicManager()->CreateCanvasClientFor(GetBufferClientType(), this, flags);

    if (!mCanvasClient) {
      return;
    }
  }
  mCanvasClient->Update(gfx::IntSize(mBounds.width, mBounds.height), this);

  FireDidTransactionCallback();
  
  mCanvasClient->Updated(BasicManager()->Hold(this));
}

void
BasicShadowCanvasLayer::Initialize(const Data& aData)
{
  NS_RUNTIMEABORT("Incompatibe surface type");
}

void
BasicShadowCanvasLayer::Swap(const SharedImage& aNewFront, bool needYFlip,
                             SharedImage* aNewBack)
{
  AutoOpenSurface autoSurface(OPEN_READ_ONLY, aNewFront);
  // Destroy mFrontBuffer if size different
  gfxIntSize sz = autoSurface.Size();
  bool surfaceConfigChanged = sz != gfxIntSize(mBounds.width, mBounds.height);
  if (IsSurfaceDescriptorValid(mFrontSurface)) {
    AutoOpenSurface autoFront(OPEN_READ_ONLY, mFrontSurface);
    surfaceConfigChanged = surfaceConfigChanged ||
                           autoSurface.ContentType() != autoFront.ContentType();
  }
  if (surfaceConfigChanged) {
    DestroyFrontBuffer();
    mBounds.SetRect(0, 0, sz.width, sz.height);
  }

  mNeedsYFlip = needYFlip;
  // If mFrontBuffer
  if (IsSurfaceDescriptorValid(mFrontSurface)) {
    *aNewBack = mFrontSurface;
  } else {
    *aNewBack = null_t();
  }
  mFrontSurface = aNewFront;
}

void
BasicShadowCanvasLayer::Paint(gfxContext* aContext, Layer* aMaskLayer)
{
  NS_ASSERTION(BasicManager()->InDrawing(),
               "Can only draw in drawing phase");

  if (!IsSurfaceDescriptorValid(mFrontSurface)) {
    return;
  }

  AutoOpenSurface autoSurface(OPEN_READ_ONLY, mFrontSurface);
  nsRefPtr<gfxPattern> pat = new gfxPattern(autoSurface.Get());

  pat->SetFilter(mFilter);
  pat->SetExtend(gfxPattern::EXTEND_PAD);

  gfxRect r(0, 0, mBounds.width, mBounds.height);

  gfxMatrix m;
  if (mNeedsYFlip) {
    m = aContext->CurrentMatrix();
    aContext->Translate(gfxPoint(0.0, mBounds.height));
    aContext->Scale(1.0, -1.0);
  }

  AutoSetOperator setOperator(aContext, GetOperator());
  aContext->NewPath();
  // No need to snap here; our transform has already taken care of it
  aContext->Rectangle(r);
  aContext->SetPattern(pat);
  FillWithMask(aContext, GetEffectiveOpacity(), aMaskLayer);

  if (mNeedsYFlip) {
    aContext->SetMatrix(m);
  }
}

already_AddRefed<CanvasLayer>
BasicLayerManager::CreateCanvasLayer()
{
  NS_ASSERTION(InConstruction(), "Only allowed in construction phase");
  nsRefPtr<CanvasLayer> layer = new BasicCanvasLayer(this);
  return layer.forget();
}

already_AddRefed<CanvasLayer>
BasicShadowLayerManager::CreateCanvasLayer()
{
  NS_ASSERTION(InConstruction(), "Only allowed in construction phase");
  nsRefPtr<BasicShadowableCanvasLayer> layer =
    new BasicShadowableCanvasLayer(this);
  MAYBE_CREATE_SHADOW(Canvas);
  return layer.forget();
}

already_AddRefed<ShadowCanvasLayer>
BasicShadowLayerManager::CreateShadowCanvasLayer()
{
  NS_ASSERTION(InConstruction(), "Only allowed in construction phase");
  nsRefPtr<ShadowCanvasLayer> layer = new BasicShadowCanvasLayer(this);
  return layer.forget();
}

}
}
