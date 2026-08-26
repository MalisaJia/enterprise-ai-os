//
//  ACRBaseCardElementRenderer
//  ACRBaseCardElementRenderer.h
//
//  Copyright © 2017 Microsoft. All rights reserved.
//
#import "ACRIBaseCardElementRenderer.h"
#import <Foundation/Foundation.h>
@interface ACRBaseCardElementRenderer : NSObject <ACRIBaseCardElementRenderer>
- (void)setSeparationConfig:(UIView *)viewGroup;

+ (void)applyLayoutStyle:(NSString *)styleFormat viewsMap:(NSDictionary *)viewsMap;

@end
