//
//  ACRColumnSetRenderer
//  ACRColumnSetRenderer.h
//
//  Copyright © 2017 Microsoft. All rights reserved.
//

#ifdef SWIFT_PACKAGE
/// Swift Package Imports
#import "ACRBaseCardElementRenderer.h"
#else
/// Cocoapods Imports
#import <AdaptiveCards/ACRBaseCardElementRenderer.h>
#endif

@interface ACRColumnSetRenderer : ACRBaseCardElementRenderer

+ (ACRColumnSetRenderer *)getInstance;

@end
