import numpy as np
import geopandas as gpd
from sklearn.metrics import make_scorer
from sklearn.neighbors import KDTree
from .base_classes import BaseSpatialCV
from .grid_builder import construct_blocks
from .utils import geometry_to_2d


# def check_data(data):

#     # check method
    
#     # check tiles scalar
    
#     # check buffer radius
    
    
#     if not isinstance(data, tuple):
#         data = (data,)
#     return data

class HBLOCK(BaseSpatialCV):
    
    def __init__(
        self,
        tiles_x=5,
        tiles_y=5,
        method='unique',
        buffer_radius=0,
        direction='diagonal',
        n_groups=5
    ):
        
        # ADD: Check data inputs function 
        
        self.tiles_x = tiles_x
        self.tiles_y = tiles_y
        self.method = method
        self.buffer_radius = buffer_radius
        self.direction = direction
        self.n_groups = n_groups
        

    def _iter_test_indices(self, X):
        tiles_x = self.tiles_x
        tiles_y = self.tiles_y
        method = self.method
        buffer_radius = self.buffer_radius
        direction = self.direction
        n_groups = self.n_groups
        
        # Convert to GDF to use Geopandas functions
        XYs = gpd.GeoDataFrame(({'geometry':X}))
                
        # Define grid type used in CV procedure
        grid = construct_blocks(XYs, 
                      tiles_x = tiles_x, 
                      tiles_y = tiles_y, 
                      method = method, 
                      direction = direction, 
                      n_groups = n_groups)
        
        # Assign pts to grids
        XYs = assign_pt_to_grid(XYs, grid)
        grid_ids = np.unique(grid.grid_id)
        
        # Yield test indices and optionally training indices within buffer
        for grid_id in grid_ids:

            test_points = XYs.loc[XYs['grid_id'] == grid_id ].index.values

            # Remove empty grids
            if len(test_points) < 1:
                continue

            # Remove training points from dead zone buffer
            if buffer_radius > 0:    
                # Buffer grid and clip training instances
                grid_poly = grid.loc[[grid_id]]
                grid_poly_buffer = grid_poly.buffer(buffer_radius)
                deadzone_points = gpd.clip(XYs, grid_poly_buffer)
                hblock_train_exclude = deadzone_points[~deadzone_points.index.isin(test_points)].index.values
                
                yield test_points, hblock_train_exclude

            else:
                # Yield empty array because no training data removed in dead zone when buffer is zero
                empty = np.array([], dtype=np.int)
                yield test_points, empty

class SLOO(BaseSpatialCV):
    
    def __init__(
        self,
        buffer_radius = None,
        shuffle = False,
        random_state = None
    ):
        self.buffer_radius = buffer_radius
        self.shuffle = shuffle
        self.random_state = random_state
    
    def _iter_test_indices(self, X):
        buffer_radius = self.buffer_radius
        sloo_n = X.shape[0]
            
        for test_index in range(sloo_n):
                        
            # Build LOO buffer
            loo_buffer = X.loc[[test_index]].centroid.buffer(buffer_radius)
    
            # Exclude training instances in dead zone buffer 
            sloo_train_exclude = gpd.clip(X, loo_buffer).index.values
            sloo_train_exclude = sloo_train_exclude[sloo_train_exclude != test_index]
            
            # Convert test instane from scalar to array (1,)
            test_index = np.array([test_index])
                
            yield test_index, sloo_train_exclude
                    
def cross_val_score(
    model,
    coordinates,
    X,
    y,
    cv,
    scoring
):
    # Fallback to (a)spatial CV if None
    if cv is None:
        cv = KFold(shuffle=True, random_state=0, n_splits=5)
    
    X = np.array(X)
    y = np.array(y)
    
    scores = []
    scorer = make_scorer(scoring)
    for train_index, test_index in cv.split(coordinates):
        model.fit(X[train_index], y[train_index])
        scores.append(        
            scorer(model, X[test_index], 
                          y[test_index])
            
        )
    scores = np.asarray(scores)    
    return scores

def assign_pt_to_grid(XYs, grid):
    
    XYs = gpd.sjoin(XYs, grid, how='left' , op='within')

    # Nan are assigned to points at grid borders, so we map nan to nearest grid
    if XYs['grid_id'].isna().any():

        grid_centroid = grid.geometry.centroid
        grid_centroid = geometry_to_2d(grid_centroid)

        border_pt_index = XYs['grid_id'].isna()
        border_pts = XYs[border_pt_index].geometry
        border_pts = geometry_to_2d(border_pts)

        tree = KDTree(grid_centroid, metric='euclidean') 
        grid_id  = tree.query(border_pts, k=1, return_distance=False)

        XYs.loc[border_pt_index, 'grid_id'] = grid_id
        XYs = XYs.drop(columns=['index_right'])

    return XYs