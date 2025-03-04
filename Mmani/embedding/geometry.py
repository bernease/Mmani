"""
Scalable Manifold learning utilities and algorithms. 

Graphs are represented with their weighted adjacency matrices, preferably using
sparse matrices.

A note on symmetrization and internal sparse representations
------------------------------------------------------------ 

For performance, this code uses the FLANN libarary to compute
approximate neighborhoods efficiently. The down side of approximation
is that (1) the distance matrix (or adjacency matrix) produced is NOT
GUARANTEED to be symmetric. We also use sparse representations, and
(2) fl_radius_neighbors_graph returns a sparse matrix called distance_matrix.

distance_matrix has 0.0 on the diagonal, as it should. Implicitly, the
missing entries are infinity not 0 for this matrix. But (1) and (2)
mean that if one tries to symmetrize distance_matrix, the scipy.sparse
code eliminates the 0.0 entries from distance_matrix. [I did not find
an efficient way around this problem.]

Hence, I adopted the following convention: 
   * distance_matrix will NOT BE GUARANTEED symmetric
   * affinity_matrix will perform a symmetrization by default
   * laplacian does NOT perform symmetrization by default, only if symmetrize=True, and DOES NOT check symmetry
   * these conventions are the same for dense matrices, for consistency

On internal sparse representations: currently the code contains some
conversions between the coo and csr formats. In the near future I plan
to clean these and use csr only.

"""
#Authors: Marina Meila <mmp@stat.washington.edu>
#         With help from Jake Vanderplas <vanderplas@astro.washington.edu>
# License: BSD 3 clause

import numpy as np
from scipy import sparse
from pyflann import *     # how to import conditionally
from sklearn.metrics.pairwise import rbf_kernel
from sklearn.neighbors import radius_neighbors_graph
from sklearn.neighbors import kneighbors_graph


from sklearn.utils.graph import graph_shortest_path

###############################################################################
# Path and connected component analysis.
# Code adapted from networkx
# Code from sklean/graph (shall we keep this here)
def single_source_shortest_path_length(graph, source, cutoff=None):
    """Return the shortest path length from source to all reachable nodes.

    Returns a dictionary of shortest path lengths keyed by target.

    Parameters
    ----------
    graph: sparse matrix or 2D array (preferably LIL matrix)
        Adjacency matrix of the graph
    source : node label
       Starting node for path
    cutoff : integer, optional
        Depth to stop the search - only
        paths of length <= cutoff are returned.

    Examples
    --------
    >>> from sklearn.utils.graph import single_source_shortest_path_length
    >>> import numpy as np
    >>> graph = np.array([[ 0, 1, 0, 0],
    ...                   [ 1, 0, 1, 0],
    ...                   [ 0, 1, 0, 1],
    ...                   [ 0, 0, 1, 0]])
    >>> single_source_shortest_path_length(graph, 0)
    {0: 0, 1: 1, 2: 2, 3: 3}
    >>> single_source_shortest_path_length(np.ones((6, 6)), 2)
    {0: 1, 1: 1, 2: 0, 3: 1, 4: 1, 5: 1}
    """
    if sparse.isspmatrix(graph):
        graph = graph.tolil()
    else:
        graph = sparse.lil_matrix(graph)
    seen = {}                   # level (number of hops) when seen in BFS
    level = 0                   # the current level
    next_level = [source]       # dict of nodes to check at next level
    while next_level:
        this_level = next_level     # advance to next level
        next_level = set()          # and start a new list (fringe)
        for v in this_level:
            if v not in seen:
                seen[v] = level     # set the level of vertex v
                next_level.update(graph.rows[v])
        if cutoff is not None and cutoff <= level:
            break
        level += 1
    return seen  # return all path lengths as dictionary


###############################################################################
# Graph laplacian
# Code adapted from the Matlab function laplacian.m of Dominique Perrault-Joncas
def graph_laplacian(csgraph, normed='geometric', symmetrize=False, scaling_epps=0., renormalization_exponent=1, return_diag=False, return_lapsym=False):
    """ Return the Laplacian matrix of an undirected graph.

   Computes a consistent estimate of the Laplace-Beltrami operator L
   from the similarity matrix A . See "Diffusion Maps" (Coifman and
   Lafon, 2006) and "Graph Laplacians and their Convergence on Random
   Neighborhood Graphs" (Hein, Audibert, Luxburg, 2007) for more
   details. 

   ????It also returns the Kth firts eigenvectors PHI of the L in
   increasing order of eigenvalues LAM.

   A is the similarity matrix from the sampled data on the manifold M.
   Typically A is obtained from the data X by applying the heat kernel 
   A_ij = exp(-||X_i-X_j||^2/EPPS). The bandwidth EPPS of the kernel is
   need to obtained the properly scaled version of L. Following the usual
   convention, the laplacian (Laplace-Beltrami operator) is defined as 
   div(grad(f)) (that is the laplacian is taken to be negative
   semi-definite).

   Note that the Laplacians defined here are the negative of what is 
   commonly used in the machine learning literature. This convention is used
   so that the Laplacians converge to the standard definition of the
   differential operator.

    Parameters
    ----------
    notation: A = csgraph, D=diag(A1) the diagonal matrix of degrees
              L = lap = returned object
              EPPS = scaling_epps**2
           
    csgraph : array_like or sparse matrix, 2 dimensions
        compressed-sparse graph, with shape (N, N). 
    normed : string, optional
        if 'renormalized':
            compute renormalized Laplacian of Coifman & Lafon
            L = D**-alpha A D**-alpha
            T = diag(L1)
            L = T**-1 L - eye()
        if 'symmetricnormalized':
           compute normalized Laplacian
            L = D**-0.5 A D**-0.5 - eye()
        if 'unnormalized': compute unnormalized Laplacian.
            L = A-D
        if 'randomwalks': compute stochastic transition matrix
            L = D**-1 A
    symmetrize: bool, optional 
        if True symmetrize adjacency matrix (internally) before computing lap
    scaling_epps: float, optional
        if >0., it should be the same neighbors_radius that was used as kernel
        width for computing the affinity. The Laplacian gets the scaled by
        4/np.sqrt(scaling_epps) in order to ensure consistency in the limit
        of large N
    return_diag : bool, optional (kept for compatibility)
        If True, then return diagonal as well as laplacian.
    return_lapsym : bool, optional
        If normed in { 'geometric', 'renormalized' } then a symmetric matrix
        lapsym, and a row normalization vector w are also returned. Having
        these allows us to compute the laplacian spectral decomposition 
        as a symmetric matrix, which has much better numerical properties. 

    Returns
    -------
    lap : ndarray
        The N x N laplacian matrix of graph.
    diag : ndarray (obsolete, for compatibiility)
        The length-N diagonal of the laplacian matrix.
        diag is returned only if return_diag is True.

    Notes
    -----
    There are a few differences from the sklearn.spectral_embedding laplacian
    function. 
    1) normed='unnormalized' and 'symmetricnormalized' correspond 
    respectively to normed=False and True in the latter. (Note also that normed
    was changed from bool to string.
    2) the signs of this laplacians are changed w.r.t the original
    3) the diagonal of lap is no longer set to 0; also there is no checking if 
    the matrix has zeros on the diagonal. If the degree of a node is 0, this
    is handled graciuously (by not dividing by 0).
    4) if csgraph is not symmetric the out-degree is used in the
    computation and no warning is raised. 
    However, it is not recommended to use this function for directed graphs.
    Use directed_laplacian() (NYImplemented) instead
    """
    if csgraph.ndim != 2 or csgraph.shape[0] != csgraph.shape[1]:
        raise ValueError('csgraph must be a square matrix or array')

    normed = normed.lower()
    if normed not in ('unnormalized', 'geometric', 'randomwalk', 'symmetricnormalized','renormalized' ):
        raise ValueError('normed must be one of unnormalized, geometric, randomwalk, symmetricnormalized, renormalized')
    if (np.issubdtype(csgraph.dtype, np.int) or np.issubdtype(csgraph.dtype, np.uint)):
        csgraph = csgraph.astype(np.float)

    if sparse.isspmatrix(csgraph):
        return _laplacian_sparse(csgraph, normed=normed, symmetrize=symmetrize, scaling_epps=scaling_epps, renormalization_exponent=renormalization_exponent, return_diag=return_diag, return_lapsym = return_lapsym)

    else:
        return _laplacian_dense(csgraph, normed=normed, symmetrize=symmetrize, scaling_epps=scaling_epps, renormalization_exponent=renormalization_exponent, return_diag=return_diag)

def _laplacian_sparse(csgraph, normed='geometric', symmetrize=True, scaling_epps=0., renormalization_exponent=1, return_diag=False, return_lapsym = False):
    n_nodes = csgraph.shape[0]
    if not csgraph.format == 'coo':
        lap = csgraph.tocoo()
    else:
        lap = csgraph.copy()
#    print( lap.getformat())
    if symmetrize:
        lapt = lap.copy()
        dum = lapt.row
        lapt.row = lapt.col
        lapt.col = dum
#        print( 'lapt', lapt.getformat())
        lap = lap + lapt # coo is converted to csr here
#        print( lap.getformat())
        lap.data /= 2.
    lap = lap.tocoo()
    diag_mask = (lap.row == lap.col)  # True/False

    degrees = np.asarray(lap.sum(axis=1)).squeeze()
    if normed == 'symmetricnormalized':
        w = np.sqrt(degrees)
        w_zeros = (w == 0)
        w[w_zeros] = 1
        lap.data /= w[lap.row]
        lap.data /= w[lap.col]
        lap.data[diag_mask] -= 1. 

    if normed == 'geometric':
        w = degrees.copy()     # normzlize one symmetrically by d
        w_zeros = (w == 0)
        w[w_zeros] = 1
        lap.data /= w[lap.row]
        lap.data /= w[lap.col]
        w = np.asarray(lap.sum(axis=1)).squeeze() #normalize again asymmetricall
        if return_lapsym:
            lapsym = lap.copy()
        lap.data /= w[lap.row]
        lap.data[diag_mask] -= 1.

    if normed == 'renormalized':
        w = degrees**renormalization_exponent;
        # same as 'geoetric' from here on
        w_zeros = (w == 0)
        w[w_zeros] = 1
        lap.data /= w[lap.row]
        lap.data /= w[lap.col]
        w = np.asarray(lap.sum(axis=1)).squeeze() #normalize again asymmetricall
        if return_lapsym:
            lapsym = lap.copy()
        lap.data /= w[lap.row]
        lap.data[diag_mask] -= 1.

    if normed == 'unnormalized':
        lap.data[diag_mask] -= degrees
    if normed == 'randomwalk':
        lap.data /= degrees[lap.row]
        lap.data[diag_mask] -= 1.
    if scaling_epps > 0.:
        lap.data *= 4/(scaling_epps**2)

    if return_diag:
        if return_lapsym:
            return lap, lap.data[diag_mask], lapsym, w
        else: 
            return lap, lap.data[diag_mask]
    elif return_lapsym:
        return lap, lapsym, w
    else:
        return lap

def _laplacian_dense(csgraph, normed='geometric', symmetrize=True, scaling_epps=0., renormalization_exponent=1, return_diag=False, return_lapsym = False):
    n_nodes = csgraph.shape[0]
    if symmetrize:
        lap = (csgraph + csgraph.T)/2.
    else:
        lap = csgraph.copy()
    degrees = np.asarray(lap.sum(axis=1)).squeeze()
    di = np.diag_indices( lap.shape[0] )  # diagonal indices

    if normed == 'symmetricnormalized':
        w = np.sqrt(degrees)
        w_zeros = (w == 0)
        w[w_zeros] = 1
        lap /= w
        lap /= w[:, np.newaxis]
        di = np.diag_indices( lap.shape[0] )
        lap[di] -= (1 - w_zeros).astype(lap.dtype)
    if normed == 'geometric':
        w = degrees.copy()     # normalize once symmetrically by d
        w_zeros = (w == 0)
        w[w_zeros] = 1
        lap /= w
        lap /= w[:, np.newaxis]
        w = np.asarray(lap.sum(axis=1)).squeeze() #normalize again asymmetricall
        if return_lapsym:
            lapsym = lap.copy()
        lap /= w[:, np.newaxis]
        lap[di] -= (1 - w_zeros).astype(lap.dtype)
    if normed == 'renormalized':
        w = degrees**renormalization_exponent;
        # same as 'geometric' from here on
        w_zeros = (w == 0)
        w[w_zeros] = 1
        lap /= w
        lap /= w[:, np.newaxis]
        w = np.asarray(lap.sum(axis=1)).squeeze() #normalize again asymmetricall
        if return_lapsym:
            lapsym = lap.copy()
        lap /= w[:, np.newaxis]
        lap[di] -= (1 - w_zeros).astype(lap.dtype)
    if normed == 'unnormalized':
        dum = lap[di]-degrees[np.newaxis,:]
        lap[di] = dum[0,:]
    if normed == 'randomwalk':
        lap /= degrees[:,np.newaxis]
        lap -= np.eye(lap.shape[0])

    if scaling_epps > 0.:
        lap *= 4/(scaling_epps**2)

    if return_diag:
        diag = np.array( lap[di] )
        if return_lapsym:
            return lap, diag, lapsym, w
        else: 
            return lap, diag
    elif return_lapsym:
        return lap, lapsym, w
    else:
        return lap

def distance_matrix( X, flindex = None, mode='radius_neighbors', 
                     neighbors_radius=None, symmetrize = True, n_neighbors=0 ):
    # DNearest neighbors has issues. TB FIXED
    if mode == 'nearest_neighbors':
        warnings.warn("Nearest neighbors currently does not work"
                      "falling back to radius neighbors")
        mode = 'radius_neighbors'

    if mode == 'radius_neighbors':
        neighbors_radius_ = (neighbors_radius
                             if neighbors_radius is not None else 1.0 / X.shape[1])   # to put another defaault value, like diam(X)/sqrt(dimensions)/10
        if flindex is not None:
            distance_matrix = fl_radius_neighbors_graph(X, neighbors_radius_, flindex, mode='distance')
        else:
            distance_matrix = radius_neighbors_graph(X, neighbors_radius_, mode='distance')
        return distance_matrix

def fl_radius_neighbors_graph( X, radius, flindex, mode = 'distance'):
    """
    Constructs a sparse distance matrix called graph in coo
    format. 
    Parameters
    ----------
    X: data matrix, array_like, shape = (n_samples, n_dimensions )
    radius: neighborhood radius, scalar
        the neighbors lying approximately within radius of a node will
        be returned. Or, in other words, all distances will be less or equal
        to radius. There will be entries in the matrix for zero distances.
        
        Attention when converting to dense: The rest of the distances
        should not be considered 0, but "large".
   
    flindex: FLANN index of the data X

    mode: string, optional
       "distance": graph contains pairwise distances
       "adjacency": grah contains 0. or 1., i.e it is an adjacency matrix

    Returns
    -------
    graph: the distance matrix, array_like, shape = (X.shape[0],X.shape[0])
           sparse coo or csr format
    
   Notes
   -----
    With approximate neiborhood search, the matrix is not
    necessarily symmetric. 

   mode = 'adjacency' not implemented yet
    """
    if radius < 0.:
        raise ValueError('neighbors_radius must be >=0.')
    nsam, ndim = X.shape
    
    graph_jindices = []
    graph_iindices = []
    graph_data = []
    for i in range( nsam ):
        jj, dd = flindex.nn_radius( X[i,:], radius )
        graph_data.append( dd )
        graph_jindices.append( jj )
        graph_iindices.append( i*np.ones( jj.shape, dtype=int ))

    graph_data = np.concatenate( graph_data )
    graph_iindices = np.concatenate( graph_iindices )
    graph_jindices = np.concatenate( graph_jindices )
    graph = sparse.coo_matrix((graph_data, (graph_iindices, graph_jindices)), shape=(nsam, nsam))
    return graph

class DistanceMatrix:

    def __init__(self, X, mode="radius_neighbors", use_flann = True, 
                 gamma=None, neighbors_radius = None, n_neighbors=None):
        self.mode = mode
        self.gamma = gamma
        self.neighbors_radius = neighbors_radius
        self.n_neighbors = n_neighbors
        self.distance_matrix = None
        if self.mode != "precomputed":
            self.X_ = X
        else:
            self.X_ = None
        if use_flann:
#            from pyflann import *
            self.flindex_ = FLANN()
            self.flparams_ = self.flindex_.build_index( X, algorithm = 'kmeans', target_precision = 0.9)
        else:
            self.flindex_ = None
            self.flparams_ = None

    @property
    def _pairwise(self):
        return self.mode == "precomputed"

    def get_neighbors_radius( self ):
        return self.neighbors_radius

    def get_distance_matrix( self, neighbors_radius = None, copy=True ):
        """ if a distance_matrix is already computed, and neighbors_radius not
        given, return the existing distance_matrix. Otherwise, recompute
        """
        if (self.distance_matrix is None) or (neighbors_radius is not None):
            if neighbors_radius is not None:
                self.neighbors_radius = neighbors_radius
            self.distance_matrix = distance_matrix(self.X_, self.flindex_, mode=self.mode, neighbors_radius=self.neighbors_radius, n_neighbors=self.n_neighbors)
        if copy:
            return self.distance_matrix.copy()
        else:
            return self.distance_matrix

"""
Symmetrizes a sparse matrix in place (coo and csr formats only)

NOTES: 
  1) if there are values of 0 or 0.0 in the sparse matrix, this operation will DELETE them. 
  2) currently, if the matrix is in coo format, the symmetrization converts
  automatically to csr. I did not find it necessary to revert to coo, as 
  the plan is to migrate away from coo in the near future.
  3 ) currently convert to coo; how to circumvent?
"""

def symmetrize_sparse( A ):
    if A.getformat() is not "coo":
        A = A.tocoo()
    A = (A + A.transpose(copy = True))/2.

# not used
def symmetrize_sparse_coo( A ):
    if A.format is not "csr":
        raise ValueError('Matrix given must be of CSR format.')
    At = A.copy()
    dum = At.row
    At.row = At.col
    At.col = dum
    A = A + At

def affinity_matrix( distances, neighbors_radius, symmetrize = True ):
    if neighbors_radius <= 0.:
        raise ValueError('neighbors_radius must be >0.')
    A = distances.copy()
    if sparse.isspmatrix( A ):
        A.data = A.data**2
        A.data = A.data/(-neighbors_radius**2)
        np.exp( A.data, A.data )
        if symmetrize:
            symmetrize_sparse( A )  # converts to CSR; deletes 0's
        else:
            pass
    else:
        A **= 2
        A /= (-neighbors_radius**2)
        np.exp(A, A)
        if symmetrize:
            A = (A+A.T)/2
            A = np.asarray( A, order="C" )  # is this necessary??
        else:
            pass
    return A
