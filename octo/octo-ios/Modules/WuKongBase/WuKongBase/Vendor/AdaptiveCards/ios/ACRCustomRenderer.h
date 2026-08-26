//
//  ACRCustomRenderer
//  ACRCustomRenderer.h
//
//  Copyright © 2017 Microsoft. All rights reserved.
//

#import "ACRBaseCardElementRenderer.h"

@interface ACRCustomRenderer : ACRBaseCardElementRenderer

@property NSObject<ACOIBaseCardElementParser> *customElementParser;

+ (ACRCustomRenderer *)getInstance;

@end
