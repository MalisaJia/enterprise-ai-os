//
//  ACRMediaRenderer
//  ACRMediaRenderer.h
//
//  Copyright © 2018 Microsoft. All rights reserved.
//

#import "ACRBaseCardElementRenderer.h"

@interface ACRMediaRenderer : ACRBaseCardElementRenderer <ACRIKVONotificationHandler>

+ (ACRMediaRenderer *)getInstance;

@end
